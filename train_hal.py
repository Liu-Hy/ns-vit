"""Script to run on the HAL cluster"""

import os
from pathlib import Path
import torch
from torch import nn
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import SubsetRandomSampler
from torch.utils.data.distributed import DistributedSampler
from torch.optim import SGD, AdamW
from torch.optim.lr_scheduler import StepLR, CosineAnnealingLR
#from torchvision import transforms
from custom_dataset import ImageFolder
import torch.distributed as dist
import argparse
from utils import *
from tqdm import tqdm
from torch.utils.data import DataLoader
import wandb


"""# Define sweep config
sweep_configuration = {
    'method': 'bayes',
    'name': 'sweep',
    'metric': {'goal': 'maximize', 'name': 'dev_acc'},
    'parameters':
    {
        'lr': {'values': [3e-5, 1e-4, 3e-4, 1e-3]},
        'lim': {'values': [0.3, 1, 3]},
        'nlr': {'values': [0.003, 0.01, 0.03, 0.1]},
        'eps': {'values': [0.001, 0.01, 0.1]},
     }
}

# Initialize sweep by passing in config. (Optional) Provide a name of the project.
sweep_id = wandb.sweep(sweep=sweep_configuration, project='nullspace')"""

def adv_train(dataloader, model, criterion, optimizer, scheduler, adv, delta_x, train_ratio, epoch, gpu):
    model.train()
    for step, batch in enumerate(dataloader):
        if step > int(train_ratio * len(dataloader)):
            break
        imgs, labels = [x.cuda(non_blocking=True) for x in batch]
        optimizer.zero_grad()
        outputs = model(imgs)
        # optimizer.zero_grad()
        loss = criterion(outputs, labels)
        if adv:
            x = model.module.patch_embed(imgs)
            x = x + delta_x.cuda(non_blocking=True)
            adv_outputs = model.module.head(encoder_forward(model, x))
            adv_loss = criterion(adv_outputs, labels)
            # adv_loss.backward()
            consistency = ((adv_outputs - outputs) ** 2).sum(dim=-1).mean()
            loss = loss + adv_loss  # + consistency
        loss.backward()
        optimizer.step()
        scheduler.step()
        if gpu == 0 and step % 20 == 0:
            if adv:
                print(
                    f'Epoch: {epoch}, Step {step}, Loss: {round(loss.item(), 4)}, Consistency_ratio: {round((consistency / (loss + adv_loss)).item(), 4)}',
                    flush=True)
            else:
                print(
                    f'Epoch: {epoch}, Step {step}, Loss: {round(loss.item(), 4)}', flush=True)


def validate(dataloader, model, criterion, val_ratio):
    loss, correct1, correct5, total = torch.zeros(4).cuda()
    model.eval()
    with torch.no_grad():
        for step, batch in enumerate(dataloader):
            if step > int(val_ratio * len(dataloader)):
                break
            samples, labels = [x.cuda(non_blocking=True) for x in batch]
            # print(labels.shape, labels.max(), labels.min())
            outputs = model(samples)
            # print(f'output shape: {outputs.shape}')
            loss += criterion(outputs, labels)
            _, preds = outputs.topk(5, -1, True, True)
            correct1 += torch.eq(preds[:, :1], labels.unsqueeze(1)).sum()
            correct5 += torch.eq(preds, labels.unsqueeze(1)).sum()
            total += samples.size(0)

    for x in [loss, correct1, correct5, total]:
        dist.reduce(x, 0)  # Should it be used the same way as hfai.dist?

    loss_val = loss.item() / dist.get_world_size() / len(dataloader)
    acc1 = 100 * correct1.item() / total.item()
    acc5 = 100 * correct5.item() / total.item()

    return acc1, loss_val



def validate_corruption(data_path, info_path, model, transform, criterion, batch_size, val_ratio):
    result = dict()
    type_errors = []
    for typ in tqdm(CORRUPTIONS, position=0, leave=True):
        type_path = data_path.joinpath(typ)
        assert type_path in list(data_path.iterdir())
        errors = []
        for s in range(1, 6):
            s_path = type_path.joinpath(str(s))
            assert s_path in list(type_path.iterdir())
            loader = prepare_loader(s_path, info_path, batch_size, transform)
            acc, _ = validate(loader, model, criterion, val_ratio)
            errors.append(100 - acc)
        type_errors.append(get_mean(errors))
    me = get_mean(type_errors)
    relative_es = [(e / al) for (e, al) in zip(type_errors, ALEX)]
    mce = 100 * get_mean(relative_es)
    result["es"] = type_errors
    result["ces"] = relative_es
    result["me"] = me
    result["mce"] = mce
    print(f"mCE: {mce:.2f}%, mean_err: {me}%", flush=True)
    return result


def prepare_loader(split_data, info_path, batch_size, transform=None):
    if isinstance(split_data, (str, Path)):
        split_data = ImageFolder(split_data, info_path, transform=transform)
    data_sampler = DistributedSampler(split_data)
    data_loader = DataLoader(split_data, batch_size=batch_size, sampler=data_sampler, num_workers=8, pin_memory=True)
    return data_loader

def main(gpu, args):
    rank = args.nr * args.gpus + gpu
    dist.init_process_group(backend='nccl',
                            init_method='env://',
                            world_size=args.world_size,
                            rank=rank)
    #run = wandb.init(project="nullspace", group="hal")
    torch.cuda.set_device(gpu)
    # 超参数设置
    epochs = 5
    train_batch_size = 16#16  # 256 for base model
    val_batch_size = 16#16
    rounds = 3
    lr = 3e-5  # When using SGD and StepLR, set to 0.001 # when AdamW and bachsize=256, 3e-4
    lim = 3
    nlr = 0.1
    eps = 0.01
    adv = True
    img_ratio = 0.1 #0.02
    train_ratio = 1. #0.1
    val_ratio = 1. #0.05
    save_path = Path("./output/hfai")
    data_path = Path("../data") #Path("/var/lib/data")
    save_path.mkdir(exist_ok=True, parents=True)

    if args.debug:
        train_batch_size, val_batch_size = 2, 2
        img_ratio, train_ratio, val_ratio = 0.001, 0.001, 0.1

    # 模型、数据、优化器
    model_name = 'vit_base_patch16_224'
    model, patch_size, img_size, model_config = get_model_and_config(model_name, variant='dat', offline=True)
    model.cuda(gpu)

    m = model_name.split('_')[1]
    setting = f'{m}_ps{patch_size}_epochs{epochs}_lr{lr}_bs{train_batch_size}_adv_{adv}_nlr{nlr}_rounds{rounds}' + \
              f'_lim{lim}_eps{eps}_imgr{img_ratio}_trainr{train_ratio}_valr{val_ratio}'
    setting_path = save_path.joinpath(setting)
    noise_path = setting_path.joinpath("noise")
    model_path = setting_path.joinpath("model")
    if gpu == 0:
        setting_path.mkdir(exist_ok=True, parents=True)
        noise_path.mkdir(exist_ok=True, parents=True)
        model_path.mkdir(exist_ok=True, parents=True)

    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(model_config['mean'], model_config['std'])])

    info_path = Path("info")

    held_out = 0.1
    data_set = ImageFolder(data_path.joinpath('imagenet/train'), info_path, train_transform)
    len_dev = int(held_out * len(data_set))
    len_train = len(data_set) - len_dev
    train_set, dev_set = torch.utils.data.random_split(data_set, (len_train, len_dev))
    train_sampler = DistributedSampler(train_set)
    train_loader = DataLoader(train_set, batch_size=train_batch_size, sampler=train_sampler, num_workers=4, pin_memory=True)
    img_loader = DataLoader(train_set, batch_size=train_batch_size, sampler=train_sampler, num_workers=4,
                            pin_memory=True)
    dev_loader = prepare_loader(dev_set, info_path, val_batch_size)

    val_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(model_config['mean'], model_config['std'])])

    criterion = nn.CrossEntropyLoss().cuda(gpu)
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = CosineAnnealingLR(optimizer, len(train_loader) * epochs)

    # Wrap the model
    model = nn.parallel.DistributedDataParallel(model, device_ids=[gpu])
    best_acc = 0.

    # 训练、验证
    start_epoch = len(list(model_path.iterdir()))
    if start_epoch > 0:
        print(f"Restore training from epoch {start_epoch}")
        checkpoint = torch.load(model_path.joinpath(str(start_epoch - 1)))
        model.module.load_state_dict(checkpoint["model_state_dict"])
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
        if rank == 0:
            best_acc = torch.load(setting_path.joinpath("best_epoch"))["best_acc"]
            print(f"Previous best acc: {best_acc}")

    delta_x = None
    for epoch in range(start_epoch, epochs):
        train_sampler.set_epoch(epoch)
        if adv:
            # delta_x = encoder_level_noise(model, img_loader, rounds, nlr, lim=lim, device=device)
            if Path.exists(noise_path.joinpath(str(epoch))):
                print(f"Loading learned noise at epoch {epoch}")
                delta_x = torch.load(noise_path.joinpath(str(epoch)))['delta_x']
            else:
                print("---- Learning noise")
                delta_x = encoder_level_epsilon_noise(model, img_loader, img_size, rounds, nlr, lim, eps, img_ratio)
                if gpu == 0:
                    torch.save({"delta_x": delta_x}, noise_path.joinpath(str(epoch)))
            if rank == 0:
                print(f"Noise norm: {round(torch.norm(delta_x).item(), 4)}")

        print("---- Training model")
        adv_train(train_loader, model, criterion, optimizer, scheduler, adv, delta_x, train_ratio)
        print("---- Validating model")
        result = dict()
        # Evaluate on held-out set
        dev_acc, _ = validate(dev_loader, model, criterion, val_ratio)
        # Evaluate on val and OOD datasets except imagenet-c
        for split in SPLITS:
            if split != "train" and not split.startswith("c-"):
                if split == "val":
                    typ_path = data_path.joinpath("imagenet", split)
                else:
                    typ_path = data_path.joinpath(split)
                val_loader = prepare_loader(typ_path, info_path, val_batch_size, val_transform)
                acc, _ = validate(val_loader, model, criterion, val_ratio)
                result[split] = acc
        # Evaluate on imagenet-c
        corruption_rs = validate_corruption(data_path.joinpath("corruption"), info_path, model, val_transform, criterion, val_batch_size, val_ratio)
        result["corruption"] = corruption_rs["mce"]
        if gpu == 0:
            torch.save({"model_name": model_name, "epoch": epoch,
                        "model_state_dict": model.module.state_dict(),
                        "optimizer_state_dict": optimizer.state_dict(),
                        "scheduler_state_dict": scheduler.state_dict(),
                        "result": result}, model_path.joinpath(str(epoch)))
            # 保存
            if rank == 0:
                print(f"Dev acc: {dev_acc}")
                total = get_mean([100 - v if k == "corruption" else v for k, v in result.items()])
                print(f"Avg performance: {total}\n", result)
                if dev_acc > best_acc:
                    best_acc = dev_acc
                    print(f'New Best Acc: {best_acc:.2f}%')
                    torch.save({"model_state_dict": model.module.state_dict(), "best_epoch": epoch, "best_acc": best_acc}, setting_path.joinpath("best_epoch"))

        """wandb.log({
            'epoch': epoch,
            'dev_acc': dev_acc,
            'total': total,
            'mce': corruption_rs["mce"],
        })"""


if __name__ == '__main__':
    #main()
    os.environ["TORCH_CPP_LOG_LEVEL"] = "INFO"
    os.environ["TORCH_DISTRIBUTED_DEBUG"] = "DETAIL"
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--nodes', default=2, type=int, metavar='N',
                        help='number of data loading workers (default: 1)')
    parser.add_argument('-g', '--gpus', default=2, type=int,
                        help='number of gpus per node')
    parser.add_argument('-nr', '--nr', default=0, type=int,
                        help='ranking within the nodes')
    parser.add_argument('-db', '--debug', action='store_true')
    args = parser.parse_args()
    args.world_size = args.gpus * args.nodes
    mp.spawn(main, args=(args,), nprocs=args.gpus)
    #wandb.agent(sweep_id, function=main, count=4)
