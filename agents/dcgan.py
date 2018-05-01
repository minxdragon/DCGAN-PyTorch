import numpy as np

from tqdm import tqdm
import shutil
import random

import torch
from torch import nn
from torch.backends import cudnn
from torch.autograd import Variable
from torchvision.utils import vutils

from graphs.models.generator import Generator
from graphs.models.discriminator import Discriminator
from graphs.losses.loss import BinaryCrossEntropy
from datasets.celebA import CelebADataLoader

from tensorboardX import SummaryWriter
from utils.metrics import AverageMeter, AverageMeterList, evaluate
from utils.misc import print_cuda_statistics

cudnn.benchmark = True

class DCGANAgent:

    def __init__(self, config):
        self.config = config

        # define models ( generator and discriminator)
        self.netG = Generator(self.config)
        self.netD = Discriminator(self.config)

        # define dataloader
        self.dataloader = CelebADataLoader(self.config)
        self.batch_size = self.config.batch_size

        # define loss
        self.loss = BinaryCrossEntropy()

        # define optimizers for both generator and discriminator
        self.optimG = torch.optim.Adam(self.netG.parameters(), lr=self.config.learning_rate, betas=(self.config.beta1, self.config.beta2))
        self.optimD = torch.optim.Adam(self.netD.parameters(), lr=self.config.learning_rate, betas=(self.config.beta1, self.config.beta2))

        # initialize counter
        self.current_epoch = 0
        self.current_iteration = 0
        self.best_valid_mean_iou = 0

        self.fixed_noise = torch.randn(self.batch_size, self.config.g_input_size, 1, 1)
        self.real_label = 1
        self.fake_label = 0

        # set cuda flag
        self.is_cuda = torch.cuda.is_available()
        if self.is_cuda and not self.config.cuda:
            print("WARNING: You have a CUDA device, so you should probably enable CUDA")

        self.cuda = self.is_cuda & self.config.cuda

        # set the manual seed for torch
        if self.config.seed is None:
            self.manual_seed = random.randint(1, 10000)
        random.seed(self.manual_seed)
        torch.manual_seed(self.manual_seed)

        if self.cuda:
            print("Program will run on *****GPU-CUDA***** ")
            torch.cuda.manual_seed_all(self.config.seed)
            print_cuda_statistics()

            self.vgg_model = self.vgg_model.cuda()
            self.model = self.model.cuda()
            self.loss = self.loss.cuda()
        else:
            print("Program will run on *****CPU***** ")

        # Model Loading from the latest checkpoint if not found start from scratch.
        self.load_checkpoint(self.config.checkpoint_file)

        # Summary Writer
        self.summary_writer = SummaryWriter(log_dir=self.config.summary_dir, comment='DCGAN')

    def load_checkpoint(self, file_name):
        filename = self.config.checkpoint_dir + file_name
        try:
            print("Loading checkpoint '{}'".format(filename))
            checkpoint = torch.load(filename)

            self.current_epoch = checkpoint['epoch']
            self.current_iteration = checkpoint['iteration']
            self.netG.load_state_dict(checkpoint['G_state_dict'])
            self.optimG.load_state_dict(checkpoint['G_optimizer'])
            self.netD.load_state_dict(checkpoint['D_state_dict'])
            self.optimD.load_state_dict(checkpoint['D_optimizer'])

            print("Checkpoint loaded successfully from '{}' at (epoch {}) at (iteration {})\n"
                  .format(self.config.checkpoint_dir, checkpoint['epoch'], checkpoint['iteration']))
        except OSError as e:
            print("No checkpoint exists from '{}'. Skipping...".format(self.config.checkpoint_dir))
            print("**First time to train**")

    def save_checkpoint(self, file_name="checkpoint.pth.tar", is_best = 0):
        state = {
            'epoch': self.current_epoch + 1,
            'iteration': self.current_iteration,
            'G_state_dict': self.netG.state_dict(),
            'G_optimizer': self.optimG.state_dict(),
            'D_state_dict': self.netD.state_dict(),
            'D_optimizer': self.optimD.state_dict()
        }
        # Save the state
        torch.save(state, self.config.checkpoint_dir + file_name)
        # If it is the best copy it to another file 'model_best.pth.tar'
        if is_best:
            shutil.copyfile(self.config.checkpoint_dir + file_name,
                            self.config.checkpoint_dir + 'model_best.pth.tar')

    def run(self):
        """
        This function will the operator
        :return:
        """
        try:
            self.train()

        except KeyboardInterrupt:
            print("You have entered CTRL+C.. Wait to finalize")

    def train(self):
        for epoch in range(self.current_epoch, self.config.max_epoch):
            self.current_epoch = epoch
            self.train_one_epoch()
            self.save_checkpoint()

    def train_one_epoch(self):
        # initialize tqdm batch
        tqdm_batch = tqdm(self.dataloader.loader, total=self.dataloader.num_iterations, desc="epoch-{}-".format(self.current_epoch))

        self.model.train()

        epoch_lossG = AverageMeter()
        epoch_lossD = AverageMeter()


        for curr_it, x in enumerate(tqdm_batch):
            y = torch.full((self.batch_size,), self.real_label)
            fake_noise = torch.randn(self.batch_size, self.config.g_input_size, 1, 1)


            if self.cuda:
                x = x.cuda(async=self.config.async_loading)
                y = y.cuda(async=self.config.async_loading)
                fake_noise = fake_noise.cuda(async=self.config.async_loading)

            x = Variable(x)

            ####################
            # Update D network #
            # train with real
            self.netD.zero_grad()
            D_real_out = self.netD(x)
            loss_D_real = self.loss(D_real_out, y)
            loss_D_real.backward()
            D_mean_real_out = D_real_out.mean().item()

            # train with fake
            G_fake_out = self.netG(fake_noise)
            y.fill_(self.fake_label)

            D_fake_out = self.netD(G_fake_out.detach())

            loss_D_fake = self.loss(D_fake_out, y)
            loss_D_fake.backward()
            D_mean_fake_out = D_fake_out.mean().item()

            loss_D = loss_D_fake + loss_D_real
            self.optimD.step()

            ####################
            # Update G network #
            self.netG.zero_grad()
            y.fill_(self.real_label)
            D_out = self.netD(G_fake_out)
            loss_G = self.loss(D_out, y)
            loss_G.backward()

            D_G_mean_out = D_out.mean().item()

            self.optimG.step()

            epoch_lossD.update(loss_D)
            epoch_lossG.update(loss_G)

            self.current_iteration += 1

            if curr_it % 100 == 0:
                self.summary_writer.add_scalar("epoch/Generator_loss", epoch_lossG.val, self.current_iteration)
                self.summary_writer.add_scalar("epoch/Discriminator_loss", epoch_lossD.val, self.current_iteration)

                self.summary_writer.add_image("Real image {}".format(curr_it),
                                              x, self.current_iteration)
                Gen_out = self.netG(self.fixed_noise)
                self.summary_writer.add_image("Generated Image{}".format(curr_it),
                                              Gen_out.detach(), self.current_iteration)

        tqdm_batch.close()

        print("Training at epoch-" + str(self.current_epoch) + " | " + "Discriminator loss: " + str(
            epoch_lossD.val) + " - Generator Loss-: " + str(epoch_lossG.val) + " - mean 1: " + str(D_mean_real_out) +
            "- mean 2: " + str(D_mean_fake_out) + " - mean 3: " + str(D_G_mean_out))


    def validate(self):
        pass

    def finalize(self):
        """
        Finalize all the operations of the 2 Main classes of the process the operator and the data loader
        :return:
        """
        print("Please wait while finalizing the operation.. Thank you")
        self.save_checkpoint()
        self.summary_writer.export_scalars_to_json("{}all_scalars.json".format(self.config.summary_dir))
        self.summary_writer.close()
        self.dataloader.finalize()
