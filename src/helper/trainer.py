
import os
# import scipy.misc
import numpy as np
import tensorflow as tf

import src.utils.viz as viz
import src.models.distribution as distribution


def display(global_step,
            step,
            scaler_sum_list,
            name_list,
            collection,
            summary_val=None,
            summary_writer=None,
            ):
    print('[step: {}]'.format(global_step), end='')
    for val, name in zip(scaler_sum_list, name_list):
        print(' {}: {:.4f}'.format(name, val * 1. / step), end='')
    print('')
    if summary_writer is not None:
        s = tf.Summary()
        for val, name in zip(scaler_sum_list, name_list):
            s.value.add(tag='{}/{}'.format(collection, name),
                        simple_value=val * 1. / step)
        summary_writer.add_summary(s, global_step)
        if summary_val is not None:
            summary_writer.add_summary(summary_val, global_step)

class Trainer(object):
    def __init__(self, train_model, generate_model, train_data, cls_valid_model=None, distr_type='gaussian',
                 use_label=False, init_lr=1e-3, save_path=None):

        self._save_path = save_path

        self._t_model = train_model
        
        self._train_data = train_data
        self._lr = init_lr
        self._use_label = use_label
        self._dist = distr_type

        self._train_op = train_model.get_reconstruction_train_op()
        self._loss_op = train_model.get_reconstruction_loss()
        self._train_summary_op = train_model.get_train_summary()
        self._valid_summary_op = train_model.get_valid_summary()

        try:
            self._train_d_op = train_model.get_latent_discrimator_train_op()
            self._train_g_op = train_model.get_latent_generator_train_op()
            self._d_loss_op = train_model.latent_d_loss
            self._g_loss_op = train_model.latent_g_loss
        except (AttributeError, KeyError):
            pass

        try:
            self._train_cat_d_op = train_model.get_cat_discrimator_train_op()
            self._train_cat_g_op = train_model.get_cat_generator_train_op()
            self._cat_d_loss_op = train_model.cat_d_loss
            self._cat_g_loss_op = train_model.cat_g_loss
        except (AttributeError, KeyError):
            pass

        try:
            self._cls_train_op = train_model.get_cls_train_op()
            self._cls_loss_op = train_model.get_cls_loss()
            self._cls_accuracy_op = train_model.get_cls_accuracy()
        except (AttributeError, KeyError):
            pass

        if generate_model is not None:
            self._g_model = generate_model
            self._generate_op = generate_model.layers['generate']
            self._generate_summary_op = generate_model.get_generate_summary()

        if cls_valid_model is not None:
            self._cls_v_model = cls_valid_model
            self._cls_valid_loss_op = cls_valid_model.get_cls_loss()
            self._cls_v_accuracy_op = cls_valid_model.get_cls_accuracy()

        self.global_step = 0
        self.epoch_id = 0

    def valid_semisupervised_epoch(self, sess, dataflow, summary_writer=None):
        dataflow.setup(epoch_val=0, batch_size=dataflow.batch_size)
        display_name_list = ['cls_loss', 'cls_accuracy']
        cur_summary = None
        step = 0
        cls_loss_sum = 0
        cls_accuracy_sum = 0
        while dataflow.epochs_completed < 1:
            step += 1
            batch_data = dataflow.next_batch_dict()
            im = batch_data['im']
            label = batch_data['label']

            cls_loss, cls_accuracy = sess.run(
                [self._cls_valid_loss_op, self._cls_v_accuracy_op],
                feed_dict={self._cls_v_model.image: im,
                           self._cls_v_model.label: label})
            cls_loss_sum += cls_loss
            cls_accuracy_sum += cls_accuracy

        print('[Valid]: ', end='')
        display(self.global_step,
                step,
                [cls_loss_sum, cls_accuracy_sum],
                display_name_list,
                'valid',
                summary_val=cur_summary,
                summary_writer=summary_writer)


    def train_semisupervised_epoch(self, sess, ae_dropout=1.0, summary_writer=None):
        label_data = self._train_data['labeled']
        unlabel_data = self._train_data['unlabeled']
        display_name_list = ['loss', 'z_d_loss', 'z_g_loss', 'y_d_loss', 'y_g_loss',
                             'cls_loss', 'cls_accuracy']
        cur_summary = None
        cur_epoch = unlabel_data.epochs_completed

        self.epoch_id += 1

        if self.epoch_id == 150:
            self._lr = self._lr / 10
        if self.epoch_id == 200:
            self._lr = self._lr / 10

        step = 0
        loss_sum = 0
        z_d_loss_sum = 0
        z_g_loss_sum = 0
        y_d_loss_sum = 0
        y_g_loss_sum = 0
        cls_loss_sum = 0
        cls_accuracy_sum = 0
        while cur_epoch == unlabel_data.epochs_completed:
            self.global_step += 1
            step += 1

            batch_data = unlabel_data.next_batch_dict()
            im = batch_data['im']
            label = batch_data['label']

            z_real_sample = distribution.diagonal_gaussian(
                len(im), self._t_model.n_code, mean=0, var=1.0)

            y_real_sample = np.random.choice(self._t_model.n_class, len(im))
            # a = np.array([1, 0, len(im)])
            # b = np.zeros((len(im), self._t_model.n_class))
            # b[np.arange(len(im)), y_real_sample] = 1
            # y_real_sample = b
            # print(y_real_sample)

            # train autoencoder
            _, loss, cur_summary = sess.run(
                [self._train_op, self._loss_op, self._train_summary_op], 
                feed_dict={self._t_model.image: im,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: ae_dropout,
                           self._t_model.label: label,
                           self._t_model.real_distribution: z_real_sample,
                           self._t_model.real_y: y_real_sample})

            # z discriminator
            _, z_d_loss = sess.run(
                [self._train_d_op, self._d_loss_op], 
                feed_dict={self._t_model.image: im,
                           # self._t_model.label: label,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 1.,
                           self._t_model.real_distribution: z_real_sample})

            # z generator
            _, z_g_loss = sess.run(
                [self._train_g_op, self._g_loss_op], 
                feed_dict={self._t_model.image: im,
                           # self._t_model.label: label,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 1.})

            # y discriminator
            _, y_d_loss = sess.run(
                [self._train_cat_d_op, self._cat_d_loss_op], 
                feed_dict={self._t_model.image: im,
                           # self._t_model.label: label,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 1.,
                           self._t_model.real_y: y_real_sample})

            # y generator
            _, y_g_loss = sess.run(
                [self._train_cat_g_op, self._cat_g_loss_op], 
                feed_dict={self._t_model.image: im,
                           # self._t_model.label: label,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 1.})

            batch_data = label_data.next_batch_dict()
            im = batch_data['im']
            label = batch_data['label']
            # semisupervise
            if self.global_step % 10 == 0:
                _, cls_loss, cls_accuracy = sess.run(
                    [self._cls_train_op, self._cls_loss_op, self._cls_accuracy_op], 
                    feed_dict={self._t_model.image: im,
                               self._t_model.label: label,
                               self._t_model.lr: self._lr,
                               self._t_model.keep_prob: 1.})
                cls_loss_sum += cls_loss
                cls_accuracy_sum += cls_accuracy
            

            loss_sum += loss
            z_d_loss_sum += z_d_loss
            z_g_loss_sum += z_g_loss
            y_d_loss_sum += y_d_loss
            y_g_loss_sum += y_g_loss
            
            
            if step % 100 == 0:
                display(self.global_step,
                    step,
                    [loss_sum, z_d_loss_sum, z_g_loss_sum, y_d_loss_sum, y_g_loss_sum,
                     cls_loss_sum * 10, cls_accuracy_sum * 10],
                    display_name_list,
                    'train',
                    summary_val=cur_summary,
                    summary_writer=summary_writer)

        print('==== epoch: {}, lr:{} ===='.format(cur_epoch, self._lr))
        display(self.global_step,
                step,
                [loss_sum, z_d_loss_sum, z_g_loss_sum, y_d_loss_sum, y_g_loss_sum],
                display_name_list,
                'train',
                summary_val=cur_summary,
                summary_writer=summary_writer)


    def train_z_gan_epoch(self, sess, ae_dropout=1.0, summary_writer=None):
        self._t_model.set_is_training(True)
        display_name_list = ['loss', 'd_loss', 'g_loss']
        cur_summary = None
        # if self.epoch_id == 50:
        #     self._lr = self._lr / 10
        # if self.epoch_id == 200:
        #     self._lr = self._lr / 10
        if self.epoch_id == 100:
            self._lr = self._lr / 10
        if self.epoch_id == 300:
            self._lr = self._lr / 10

        cur_epoch = self._train_data.epochs_completed

        step = 0
        loss_sum = 0
        d_loss_sum = 0
        g_loss_sum = 0
        self.epoch_id += 1
        while cur_epoch == self._train_data.epochs_completed:
            self.global_step += 1
            step += 1

            # batch_data = self._train_data.next_batch_dict()
            # im = batch_data['im']
            # label = batch_data['label']

            # _, d_loss = sess.run(
            #     [self._train_d_op, self._d_loss_op], 
            #     feed_dict={self._t_model.image: im,
            #                self._t_model.lr: self._lr,
            #                self._t_model.keep_prob: 1.})

            batch_data = self._train_data.next_batch_dict()
            im = batch_data['im']
            label = batch_data['label']

            if self._use_label:
                label_indices = label
            else:
                label_indices = None

            if self._dist == 'gmm':
                real_sample = distribution.gaussian_mixture(
                    len(im), n_dim=self._t_model.n_code, n_labels=10,
                    x_var=0.5, y_var=0.1, label_indices=label_indices)
            else:
                real_sample = distribution.diagonal_gaussian(
                    len(im), self._t_model.n_code, mean=0, var=1.0)

            # train autoencoder
            _, loss, cur_summary = sess.run(
                [self._train_op, self._loss_op, self._train_summary_op], 
                feed_dict={self._t_model.image: im,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: ae_dropout,
                           self._t_model.label: label,
                           self._t_model.real_distribution: real_sample})

            # train discriminator
            
            _, d_loss = sess.run(
                [self._train_d_op, self._d_loss_op], 
                feed_dict={self._t_model.image: im,
                           self._t_model.label: label,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 1.,
                           self._t_model.real_distribution: real_sample})

            # train generator
            _, g_loss = sess.run(
                [self._train_g_op, self._g_loss_op], 
                feed_dict={self._t_model.image: im,
                           self._t_model.label: label,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 1.})

            # batch_data = self._train_data.next_batch_dict()
            # im = batch_data['im']
            # label = batch_data['label']
            loss_sum += loss
            d_loss_sum += d_loss
            g_loss_sum += g_loss

            if step % 100 == 0:
                display(self.global_step,
                    step,
                    [loss_sum, d_loss_sum, g_loss_sum],
                    display_name_list,
                    'train',
                    summary_val=cur_summary,
                    summary_writer=summary_writer)

        print('==== epoch: {}, lr:{} ===='.format(cur_epoch, self._lr))
        display(self.global_step,
                step,
                [loss_sum, d_loss_sum, g_loss_sum],
                display_name_list,
                'train',
                summary_val=cur_summary,
                summary_writer=summary_writer)

    def train_epoch(self, sess, summary_writer=None):
        self._t_model.set_is_training(True)
        display_name_list = ['loss']
        cur_summary = None

        cur_epoch = self._train_data.epochs_completed

        step = 0
        loss_sum = 0
        self.epoch_id += 1
        while cur_epoch == self._train_data.epochs_completed:
            self.global_step += 1
            step += 1

            batch_data = self._train_data.next_batch_dict()
            im = batch_data['im']
            label = batch_data['label']
            _, loss, cur_summary = sess.run(
                [self._train_op, self._loss_op, self._train_summary_op], 
                feed_dict={self._t_model.image: im,
                           self._t_model.lr: self._lr,
                           self._t_model.keep_prob: 0.9})

            loss_sum += loss

            if step % 100 == 0:
                display(self.global_step,
                    step,
                    [loss_sum],
                    display_name_list,
                    'train',
                    summary_val=cur_summary,
                    summary_writer=summary_writer)

        print('==== epoch: {}, lr:{} ===='.format(cur_epoch, self._lr))
        display(self.global_step,
                step,
                [loss_sum],
                display_name_list,
                'train',
                summary_val=cur_summary,
                summary_writer=summary_writer)

    def valid_epoch(self, sess, dataflow=None, moniter_generation=False, summary_writer=None):
        # self._g_model.set_is_training(True)
        # display_name_list = ['loss']
        # cur_summary = None
        
        dataflow.setup(epoch_val=0, batch_size=dataflow.batch_size)
        display_name_list = ['loss']

        step = 0
        loss_sum = 0
        while dataflow.epochs_completed == 0:
            step += 1

            batch_data = dataflow.next_batch_dict()
            im = batch_data['im']
            label = batch_data['label']
            loss, valid_summary = sess.run(
                [self._loss_op, self._valid_summary_op],
                feed_dict={self._t_model.encoder_in: im,
                           self._t_model.image: im,
                           self._t_model.keep_prob: 1.0,
                           self._t_model.label: label,
                           })
            loss_sum += loss

        print('[Valid]: ', end='')
        display(self.global_step,
                step,
                [loss_sum],
                display_name_list,
                'valid',
                summary_val=None,
                summary_writer=summary_writer)
        dataflow.setup(epoch_val=0, batch_size=dataflow.batch_size)

        gen_im = sess.run(self._generate_op)
        if moniter_generation and self._save_path:
            im_save_path = os.path.join(self._save_path,
                                        'generate_step_{}.png'.format(self.global_step))
            viz.viz_batch_im(batch_im=gen_im, grid_size=[10, 10],
                             save_path=im_save_path, gap=0, gap_color=0,
                             shuffle=False)
        if summary_writer:
            cur_summary = sess.run(self._generate_summary_op)
            summary_writer.add_summary(cur_summary, self.global_step)
            summary_writer.add_summary(valid_summary, self.global_step)
