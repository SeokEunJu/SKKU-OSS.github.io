import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.optim as optim
from torchvision.models import resnet50
import cv2
import os
from tensorboardX import SummaryWriter

from utils import Dataset, normalization, compute_gradient_penalty
from model import Generator, Discriminator, load_FAN, upsample

proj_directory = './'
data_directory = '/dataset'

celeba = os.path.join(data_directory, 'img_align_celeba')
ffhq = os.path.join(data_directory, 'ffhq')
menpo = os.path.join(data_directory, 'LS3D-W/Menpo-3D')
_300w = os.path.join(data_directory, 'LS3D-W/300W-Testset-3D')
aflw = os.path.join(data_directory, 'LS3D-W/AFLW2000-3D-Reannotated')

validation_directory = ffhq + '/00000/00043.png'

save_path_G = os.path.join(proj_directory, 'ckpt', 'generator.pth')
save_path_D = os.path.join(proj_directory, 'ckpt', 'discriminator.pth')

batch_size = 16
resnet = resnet50(pretrained=True).eval()

def train(train_directories, n_epoch):
    print('start')

    dataset = Dataset(train_directories)
    loaded_training_data = DataLoader(
        dataset=dataset, batch_size=batch_size, shuffle=True
    )

    validation_directories = [validation_directory]
    valid_dataset = Dataset(validation_directories)  # evaluation dataset
    loaded_valid_data = DataLoader(dataset=valid_dataset, batch_size=1)

    generator = Generator().cuda()
    discriminator = Discriminator().cuda()
    FAN = load_FAN().cuda()
    preprocess_for_FAN = upsample().cuda()

    if not os.path.exists(os.path.join(proj_directory, 'validation')):
        os.makedirs(os.path.join(proj_directory, 'validation'))
    if not os.path.exists(os.path.join(proj_directory, 'ckpt')):
        os.makedirs(os.path.join(proj_directory, 'ckpt'))
    if not os.path.exists(os.path.join(proj_directory, 'logs')):
        os.makedirs(os.path.join(proj_directory, 'logs'))
    summary_writer = SummaryWriter('./logs/')

    if os.path.exists(save_path_G):
        generator.load_state_dict(torch.load(save_path_G))
        print('reading generator checkpoints...')
    if os.path.exists(save_path_D):
        discriminator.load_state_dict(torch.load(save_path_D))
        print('reading discriminator checkpoints...')

    mse = nn.MSELoss()

    res_base = nn.Sequential(resnet.conv1, resnet.bn1, resnet.relu, resnet.maxpool)
    res_B1 = nn.Sequential(*list(res_base), *list(resnet.layer1)).cuda()
    res_B2 = nn.Sequential(*list(resnet.layer2)).cuda()
    res_B3 = nn.Sequential(*list(resnet.layer3)).cuda()

    init_lr = 2.5e-4
    final_lr = 1e-5
    decay = (init_lr - final_lr) / n_epoch
    GAN_start = 60

    print('train with MSE and perceptual loss')
    for epoch in range(60, n_epoch):
        # decay learning rate after one epoch
        learning_rate = init_lr - decay * epoch

        G_optimizer = optim.RMSprop(generator.parameters(), lr=learning_rate)
        D_optimizer = optim.RMSprop(discriminator.parameters(), lr=learning_rate)

        if epoch == 60:
            print('start training with additional adversarial loss')

        for i, data in enumerate(loaded_training_data):
            lr, gt, img_name = data

            # forwarding
            sr = generator(lr)

            # initialization
            G_optimizer.zero_grad()

            # forwarding through ResNet to compute Perceptual loss
            sr_b1 = res_B1(sr)
            sr_b2 = res_B2(sr_b1)
            sr_b3 = res_B3(sr_b2)

            gt_b1 = res_B1(gt).detach()
            gt_b2 = res_B2(gt_b1)
            gt_b3 = res_B3(gt_b2)

            # forward through FAN to compute FAN loss
            sr_FAN = FAN(preprocess_for_FAN(sr))[-1]
            gt_FAN = FAN(preprocess_for_FAN(gt))[-1].detach()

            # loss computation
            mse_loss = mse(sr, gt).mean()
            perceptual_loss = mse(sr_b1, gt_b1).mean() + mse(sr_b2, gt_b2).mean() + mse(sr_b3, gt_b3).mean()
            FAN_loss = mse(sr_FAN, gt_FAN)

            adv_loss = 0
            g_loss = mse_loss + perceptual_loss + FAN_loss

            # adversarial loss added in the latter epoch
            if epoch >= GAN_start:
                discriminator.eval()
                fake_logit = discriminator(sr).mean()
                adv_loss = -fake_logit
                g_loss += 1e-3 * adv_loss

            # g_loss.backward(retain_graph=True)
            g_loss.backward()
            G_optimizer.step()

            #
            # train Discriminator
            #
            d_loss = 0
            if epoch >= GAN_start:
                generator.eval()
                discriminator.train()

                G_optimizer.zero_grad()
                D_optimizer.zero_grad()

                sr = generator(lr).detach()
                fake_logit = discriminator(sr).mean()
                real_logit = discriminator(gt).mean()

                gradient_penalty = compute_gradient_penalty(discriminator, gt, sr)
                d_loss = fake_logit - real_logit + 10. * gradient_penalty
                # d_loss = 1e-3 * d_loss

                d_loss.backward()
                D_optimizer.step()
                generator.train()

            if i % 10 == 0:
                print("loss at %d : %d ==>\t%.4f (%.4f + %.4f + %4f + %.4f)\tD:%.4f"
                      % (epoch, i, g_loss, mse_loss, perceptual_loss, FAN_loss, adv_loss, d_loss))
                summary_writer.add_scalar('mse_loss', mse_loss.item(), epoch * len(loaded_training_data) + i)
                summary_writer.add_scalar('perceptual_loss', perceptual_loss.item(),
                                          epoch * len(loaded_training_data) + i)
                summary_writer.add_scalar('FAN_loss', FAN_loss.item(), epoch * len(loaded_training_data) + i)
                summary_writer.add_scalar('adv_loss', adv_loss.item(), epoch * len(loaded_training_data) + i)
                summary_writer.add_scalar('Discriminator_loss', d_loss.item(), epoch * len(loaded_training_data) + i)

        if epoch % 2 == 0:
            validation = os.path.join(proj_directory, 'validation', str(epoch))
            os.makedirs(validation)
            for _, val_data in enumerate(loaded_valid_data):
                lr, _, img_name = val_data
                sr = generator(lr)
                sr = sr[0]
                sr = normalization(sr, _from=(0, 1))
                sr = sr.cpu().detach().numpy().transpose(1, 2, 0)
                img_name = img_name[0]

                filename = os.path.join(validation, img_name)
                cv2.imwrite(filename=filename, img=sr)

            # save checkpoints
            torch.save(generator.state_dict(), save_path_G)
            torch.save(discriminator.state_dict(), save_path_D)

    # save checkpoints
    torch.save(generator.state_dict(), save_path_G)
    torch.save(discriminator.state_dict(), save_path_D)
    print('training finished.')


if __name__ == '__main__':
    epochs = 70
    train_directories = [ffhq]
    train(train_directories, epochs)
