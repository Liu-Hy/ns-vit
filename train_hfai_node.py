"""Script to run on the HFAI server"""
import haienv
haienv.set_env('ns')

import os
from pathlib import Path
import torch
from torch import nn
from torch.utils.data import SubsetRandomSampler
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
# from torchvision import transforms
from ImageNetDG import ImageNetDG

from utils import *
from tqdm import tqdm

import hfai
from ffrecord.torch import DataLoader
from ffrecord.torch.dataset import Subset
dist.set_nccl_opt_level(dist.HFAI_NCCL_OPT_LEVEL.AUTO)

#hfai.nn.functional.set_replace_torch()

def adv_train(dataloader, model, criterion, optimizer, scheduler, adv, delta_x, train_ratio, epoch, start_step, best_acc):
    model.train()
    for step, batch in enumerate(dataloader):
        step += start_step
        if step > int(train_ratio * len(dataloader)):
            break
        imgs, labels = [x.cuda(non_blocking=True) for x in batch]
        optimizer.zero_grad()
        outputs = model(imgs)
        # optimizer.zero_grad()
        loss = criterion(outputs, labels)
        if adv:
            x = model.patch_embed(imgs)
            x = x + delta_x
            adv_outputs = model.head(encoder_forward(model, x))
            adv_loss = criterion(adv_outputs, labels)
            # adv_loss.backward()
            consistency = ((adv_outputs - outputs) ** 2).sum(dim=-1).mean()
            loss = loss + adv_loss  # + consistency
        loss.backward()
        optimizer.step()
        scheduler.step()
        if step % 20 == 0:
            if adv:
                print(
                    f'Epoch: {epoch}, Step {step}, Loss: {round(loss.item(), 4)}, Consistency_ratio: {round((consistency / (loss + adv_loss)).item(), 4)}',
                    flush=True)
            else:
                print(
                    f'Epoch: {epoch}, Step {step}, Loss: {round(loss.item(), 4)}', flush=True)
        if step % 100 == 0:
            model.try_save(epoch, step + 1, others=(best_acc, delta_x), force=True)


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

    loss_val = loss.item() / len(dataloader)
    acc1 = 100 * correct1.item() / total.item()
    acc5 = 100 * correct5.item() / total.item()

    return acc1, loss_val


def validate_corruption(model, transform, criterion, batch_size, val_ratio):
    result = dict()
    type_errors = []
    for typ in CORRUPTIONS:
        errors = []
        for s in range(1, 6):
            split = "c-" + typ + "-" + str(s)
            loader = prepare_loader(split, batch_size, transform)
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

def prepare_loader(split_data, batch_size, transform=None):
    if isinstance(split_data, str):
        split_data = ImageNetDG(split_data, transform=transform)
    data_loader = DataLoader(split_data, batch_size=batch_size, shuffle=True, num_workers=4, pin_memory=True)
    return data_loader

def main():
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(device)
    # 超参数设置
    epochs = 10
    train_batch_size = 24  # 256 for base model
    val_batch_size = 24
    lr = 1e-4  # When using SGD and StepLR, set to 0.001
    rounds, nlr, lim = 3, 0.1, 3  # lim=1.0, nlr=0.02
    eps = 0.01  # 0.001
    adv = True
    img_ratio = 0.1
    train_ratio = 1
    val_ratio = 1
    save_path = Path("output/hfai")
    data_path = Path("/var/lib/data")
    save_path.mkdir(exist_ok=True, parents=True)

    # 模型、数据、优化器
    model_name = 'vit_base_patch16_224'
    model, patch_size, img_size, model_config = get_model_and_config(model_name, variant='dat', offline=True)
    model.cuda()
    #model = hfai.nn.to_hfai(model)
    model = nn.DataParallel(model)

    m = model_name.split('_')[1]
    setting = f'{m}_ps{patch_size}_epochs{epochs}_lr{lr}_bs{train_batch_size}_adv_{adv}_nlr{nlr}_rounds{rounds}' + \
              f'_lim{lim}_eps{eps}_imgr{img_ratio}_trainr{train_ratio}_valr{val_ratio}'
    setting_path = save_path.joinpath(setting)
    noise_path = setting_path.joinpath("noise")
    model_path = setting_path.joinpath("model")
    setting_path.mkdir(exist_ok=True, parents=True)
    noise_path.mkdir(exist_ok=True, parents=True)
    model_path.mkdir(exist_ok=True, parents=True)

    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(model_config['mean'], model_config['std'])])

    dataset = hfai.datasets.ImageNet('train', transform=train_transform)
    data_size = len(dataset)
    print(f"data set size: {data_size}")
    cutoff = int(0.9 * data_size)
    rand_idx = torch.randperm(data_size)
    train_indices, dev_indices = rand_idx[:cutoff], rand_idx[cutoff:]
    train_set = Subset(dataset, train_indices)
    dev_set = Subset(dataset, dev_indices)

    train_loader = DataLoader(train_set, train_batch_size, shuffle=True, num_workers=4, pin_memory=True)
    img_loader = DataLoader(train_set, train_batch_size, shuffle=True, num_workers=4, pin_memory=True)
    dev_loader = DataLoader(dev_set, val_batch_size, shuffle=False, num_workers=4, pin_memory=True)

    print(f"splitting length: train set: {len(train_set)}, dev set: {len(dev_set)}")

    val_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(model_config['mean'], model_config['std'])])
    val_set = hfai.datasets.ImageNet('val', transform=val_transform)
    val_loader = DataLoader(val_set, val_batch_size, shuffle=True, num_workers=4, pin_memory=True)

    criterion = nn.CrossEntropyLoss()
    optimizer = AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = CosineAnnealingLR(optimizer, len(train_loader) * epochs)

    ckpt_path = save_path.joinpath('latest.pt')
    try:
        start_epoch, start_step, others = hfai.checkpoint.init(model, optimizer, scheduler=scheduler, ckpt_path=ckpt_path)
    except RuntimeError:
        start_epoch, start_step, others = 0, 0, None
        print("Failed to load checkpoint, start from scratch instead.")
    best_acc, delta_x = 0., None
    if others is not None:
        best_acc, delta_x = others
        delta_x = delta_x.to(device)

    # 训练、验证
    for epoch in range(start_epoch, epochs):
        train_loader.set_step(start_step)
        if adv:
            if delta_x is None:
                print("---- Learning noise")
                delta_x = encoder_level_epsilon_noise(model, img_loader, img_size, rounds, nlr, lim, eps, img_ratio)
            print(f"Noise norm: {round(torch.norm(delta_x).item(), 4)}")

        print("---- Training model")
        adv_train(train_loader, model, criterion, optimizer, scheduler, adv, delta_x, train_ratio, epoch, start_step, best_acc)
        start_step = 0
        delta_x = None
        print("---- Validating model")
        result = dict()
        # Evaluate on held-out set
        dev_acc, _ = validate(dev_loader, model, criterion, val_ratio)
        # Evaluate on val set
        val_acc, _ = validate(val_loader, model, criterion, val_ratio)
        result["val"] = val_acc

        torch.save({"model_name": model_name, "epoch": epoch,
                    "model_state_dict": model.state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                    "scheduler_state_dict": scheduler.state_dict(),
                    "result": result}, model_path.joinpath(str(epoch)))
        # 保存
        print(f"Dev acc: {dev_acc}")
        total = get_mean([100 - v if k == "corruption" else v for k, v in result.items()])
        print(f"Avg performance: {total}\n", result)
        if dev_acc > best_acc:
            best_acc = dev_acc
            print(f'New Best Acc: {best_acc:.2f}%')
            torch.save(model.state_dict(),
                       os.path.join(save_path, 'best.pt'))


if __name__ == '__main__':
    main()
