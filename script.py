from __future__ import print_function
import os
import time
import random
import argparse
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as datasets
import torchvision.transforms as transforms
import torchvision.utils as visionutils
from torch.autograd import Variable

import gan
from gan import weight_init

parser = argparse.ArgumentParser()
parser.add_argument('--dataRoot', default='data', help='path to dataset')
parser.add_argument('--workers', type=int, default=2, help='number of data loading workers')
parser.add_argument('--batch_size', type=int, default=64, help='input batch size')
parser.add_argument('--image_size', type=int, default=64, help='the height / width of the input image to network')
parser.add_argument('--nz', type=int, default=100, help='size of the latent z vector')
parser.add_argument('--ngf', type=int, default=64)
parser.add_argument('--ndf', type=int, default=64)
parser.add_argument('--niter', type=int, default=1000, help='number of epochs to train for')
parser.add_argument('--lr', type=float, default=0.0002, help='learning rate, default=0.0002')
parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for adam. default=0.5')
parser.add_argument('--cuda'  , action='store_true', help='enables cuda')
parser.add_argument('--ngpu'  , type=int, default=1, help='number of GPUs to use')
parser.add_argument('--netG', default='', help="path to netG (to continue training)")
parser.add_argument('--netD', default='', help="path to netD (to continue training)")
parser.add_argument('--outDir', default='.', help='folder to output images and model checkpoints')
parser.add_argument('--d_labelSmooth', type=float, default=0, help='for D, use soft label "1-labelSmooth" for real samples')
parser.add_argument('--n_extra_layers_d', type=int, default=0, help='number of extra conv layers in D')
parser.add_argument('--n_extra_layers_g', type=int, default=1, help='number of extra conv layers in G')
parser.add_argument('--binary', action='store_true', help='z from bernoulli distribution, with prob=0.5')

arg_list = [
    '--dataRoot', './anime-faces',
    '--workers', '2',
    '--batch_size', '64',
    '--image_size', '64',
    '--nz', '100',
    '--ngf', '64',
    '--ndf', '64',
    '--niter', '1000',
    '--lr', '0.0002',
    '--beta1', '0.5',
    '--cuda', 
    '--ngpu', '1',
    '--netG', '',
    '--netD', '',
    '--outDir', './results',
    '--d_labelSmooth', '0.12', # 0.25 from imporved-GAN paper 
    '--n_extra_layers_d', '0',
    '--n_extra_layers_g', '1', # in the sense that generator should be more powerful
    '--binary'
]
args = parser.parse_args()
opt = parser.parse_args(arg_list)
print(opt)


try:
    os.makedirs(opt.outDir)
except OSError:
    pass

opt.manualSeed = random.randint(1,10000)
random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)

cudnn.benchmark = True

if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

nc = 3
ngpu = opt.ngpu
nz = opt.nz
ngf = opt.ngf
ndf = opt.ndf

n_extra_d = opt.n_extra_layers_d
n_extra_g = opt.n_extra_layers_g

dataset = datasets.ImageFolder(root=opt.dataRoot, transform=transforms.Compose([
    transforms.Resize(opt.image_size),
    transforms.ToTensor(),
    transforms.Normalize((0.5,0.5,0.5), (0.5,0.5,0.5))
]))

dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batch_size, shuffle=True, num_workers=opt.workers)

netG = gan._netG(ngpu, nz, nc, ngf)
netD = gan._netD(ngpu, nz, nc, ngf)

netG.apply(weight_init)
if opt.netG != '':
    netG.load_state_dict(torch.load(opt.netG))
print(netG)

netD.apply(weight_init)
if opt.netD != '':
    netD.load_state_dict(torch.load(opt.netD))
print(netD)

criterion = nn.BCELoss()
criterion_MSE = nn.MSELoss()

input = torch.FloatTensor(opt.batch_size, 3, opt.image_size, opt.image_size)
noise = torch.FloatTensor(opt.batch_size, nz, 1, 1)

if opt.binary:
    bernoulli_prob = torch.FloatTensor(opt.batch_size, nz, 1, 1).fill_(0.5)
    fixed_noise = torch.bernoulli(bernoulli_prob)
else:
    fixed_noise = torch.FloatTensor(opt.batch_size, nz, 1, 1).normal_(0, 1)
label = torch.FloatTensor(opt.batch_size)

real_label = 1
fake_label = 0

if opt.cuda:
    netD.cuda()
    netG.cuda()
    criterion.cuda()
    criterion_MSE.cuda()
    input, label = input.cuda(), label.cuda()
    noise, fixed_noise = noise.cuda(), fixed_noise.cuda()

input = Variable(input)
label = Variable(label)

noise = Variable(noise)
fixed_noise = Variable(fixed_noise)

# setup optimizer
optimizerD = optim.Adam(netD.parameters(), lr = opt.lr, betas = (opt.beta1, 0.999))
optimizerG = optim.Adam(netG.parameters(), lr = opt.lr, betas = (opt.beta1, 0.999))

device = torch.device("cuda:0" if (torch.cuda.is_available() and ngpu > 0) else "cpu")

for epoch in range(opt.niter):
    for i, data in enumerate(dataloader, 0):
        start_iter = time.time()
        # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
        
        netD.zero_grad()
        real_cpu = data[0].to(device)
        batch_size = real_cpu.size(0)
        label = torch.full((batch_size,),real_label-opt.d_labelSmooth, device=device)
        output = netD(real_cpu).view(-1)
        errD_real = criterion(output, label)
        errD_real.backward()

        D_x = output.data.mean()
       	with torch.no_grad():
	        noise.resize_(batch_size, nz, 1, 1)
        if opt.binary:
            bernoulli_prob.resize_(noise.data.size())
            noise.data.copy_(2*(torch.bernoulli(bernoulli_prob)-0.5))
        else:
            noise.data.normal_(0, 1)
        fake,z_prediction = netG(noise)
        label.fill_(fake_label)
        output = netD(fake.detach()).view(-1) # add ".detach()" to avoid backprop through G
        errD_fake = criterion(output, label)
        errD_fake.backward() # gradients for fake/real will be accumulated
        D_G_z1 = output.data.mean()
        errD = errD_real + errD_fake
        optimizerD.step() # .step() can be called once the gradients are computed

        # (2) Update G network: maximize log(D(G(z)))
        netG.zero_grad()
        label.fill_(real_label) # fake labels are real for generator cost
        output = netD(fake).view(-1)
        errG = criterion(output, label)
        errG.backward(retain_graph=True) # True if backward through the graph for the second time
         # with z predictor
        errG_z = criterion_MSE(z_prediction, noise)
        errG_z.backward()
        D_G_z2 = output.data.mean()
        optimizerG.step()
        
        end_iter = time.time()

        print('[%d/%d][%d/%d] Loss_D: %.4f Loss_G: %.4f D(x): %.4f D(G(z)): %.4f / %.4f Elapsed %.2f s'
              % (epoch, opt.niter, i, len(dataloader),
                 errD.item(), errG.item(), D_x, D_G_z1, D_G_z2, end_iter-start_iter))
        if i % 100 == 0:
            # the first 64 samples from the mini-batch are saved.
            visionutils.save_image(real_cpu[0:64,:,:,:],
                    '%s/real_samples.png' % opt.outDir, nrow=8)
            fake,_ = netG(fixed_noise)
            visionutils.save_image(fake.data[0:64,:,:,:],
                    '%s/fake_samples_epoch_%03d.png' % (opt.outDir, epoch), nrow=8)
    if epoch % 1 == 0:
        # do checkpointing
        torch.save(netG.state_dict(), '%s/netG_epoch_%d.pth' % (opt.outDir, epoch))
        torch.save(netD.state_dict(), '%s/netD_epoch_%d.pth' % (opt.outDir, epoch))


