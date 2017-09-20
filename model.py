from __future__ import division, print_function

import tensorflow as tf
import numpy as np
from vgg_normalised import vgg_from_t7
from keras import backend as K
from keras.models import Model
from keras.layers import Input, UpSampling2D, Lambda
from ops import pad_reflect, Conv2DReflect, torch_decay
from ops import wct_tf
import functools
from collections import namedtuple


### Helpers ###
mse = tf.losses.mean_squared_error
clip = lambda x: tf.clip_by_value(x, 0, 1)
EncoderDecoder = namedtuple('EncoderDecoder', 
                            'content_input content_encoder_model content_encoded \
                             style_encoded \
                             decoder_input, decoder_model decoded decoded_encoded \
                             pixel_loss feature_loss tv_loss total_loss \
                             train_op learning_rate global_step \
                             summary_op')

### Model Graph ###

class WCTModel(object):
    '''Adaptive Instance Normalization model from https://arxiv.org/abs/1703.06868'''

    def __init__(self, mode='train', relu_targets=['relu5_1','relu4_1','relu3_1','relu2_1','relu1_1'], vgg_path=None,  *args, **kwargs):
        self.mode = mode

        self.style_input = tf.placeholder_with_default(tf.constant([[[[0.,0.,0.]]]]), shape=(None, None, None, 3), name='style_img')

        # Flag for applying WCT, should only be True for test mode. Setting to False will pass through content encoding.
        self.apply_wct =  tf.placeholder_with_default(tf.constant(False), shape=[])

        self.alpha = tf.placeholder_with_default(1., shape=[], name='alpha')

        self.encoder_decoders = []
        
        ### Build the graph ###
        
        # Load shared VGG model up to deepest target layer
        with tf.name_scope('vgg_encoder'):
            deepest_target = sorted(relu_targets)[-1]
            print('Loading VGG up to layer',deepest_target)
            self.vgg_model = vgg_from_t7(vgg_path, target_layer=deepest_target)
            print(self.vgg_model.summary())

        if self.mode == 'train':
            style_encodings = [None]  # Style encoding is not needed for train stage
        else:
            # Build model to extract intermediate relu layers for style img to be used in multi-level pipeline
            with tf.name_scope('style_encoder'):
                style_encoding_layers = [self.vgg_model.get_layer(relu).output for relu in relu_targets]
                style_encoder_model = Model(inputs=self.vgg_model.input, outputs=style_encoding_layers)
                style_encodings = style_encoder_model(self.style_input)

            if len(relu_targets) == 1:
                style_encodings = [style_encodings]

        # Build enc/decs for each target relu and hook the out of each decoder up to subsequent encoder in
        for relu, style_encoded in zip(relu_targets, style_encodings):
            print('Building decoder for relu target',relu)
            
            if relu == relu_targets[0]: 
                # Input tensor will be a placeholder for the first decoder
                input_tensor = None
            else:
                # Input to intermediate levels is the (clipped) output from previous decoder
                input_tensor = clip(self.encoder_decoders[-1].decoded)
            enc_dec = self.build_model(relu, input_tensor=input_tensor, style_encoded_tensor=style_encoded, **kwargs)
        
            self.encoder_decoders.append(enc_dec)

        # Hooks for placeholder input for first encoder and final output from last decoder
        self.content_input  = self.encoder_decoders[0].content_input
        self.decoded_output = self.encoder_decoders[-1].decoded
        
    def build_model(self, 
                    relu_target,
                    input_tensor,
                    style_encoded_tensor=None,
                    batch_size=8,
                    feature_weight=1e-2,
                    pixel_weight=1,
                    tv_weight=0,
                    learning_rate=1e-4,
                    lr_decay=5e-5):
        with tf.name_scope('encoder_decoder_'+relu_target):

            ### Build encoder for reluX_1
            with tf.name_scope('content_encoder_'+relu_target):
                if input_tensor is None:  # This is the first level encoder that takes original content imgs
                    content_imgs = tf.placeholder_with_default(tf.constant([[[[0.,0.,0.]]]]), shape=(None, None, None, 3), name='content_imgs')
                else:                     # This is an intermediate-level encoder that takes output tensor from previous level as input
                    content_imgs = input_tensor  

                # Build content layer encoding model
                content_layer = self.vgg_model.get_layer(relu_target).output
                content_encoder_model = Model(inputs=self.vgg_model.input, outputs=content_layer)

                # Setup content layer encodings for content images
                content_encoded = content_encoder_model(content_imgs)
 
            ### Build style encoder & WCT if test mode
            if self.mode != 'train':                
                # Apply WCT if flag is set to true. Otherwise, pass content_encoded along
                with tf.name_scope('wct_'+relu_target):
                    decoder_input = tf.cond(self.apply_wct, lambda: wct_tf(content_encoded, style_encoded_tensor, self.alpha), lambda: content_encoded)
            else:
                decoder_input = content_encoded

            ### Build decoder
            with tf.name_scope('decoder_'+relu_target):
                n_channels = content_encoded.get_shape()[-1].value
                decoder_model = self.build_decoder(input_shape=(None, None, n_channels), relu_target=relu_target)

                # Wrap the decoder_input tensor so that it has the proper shape for decoder_model
                decoder_input_wrapped = tf.placeholder_with_default(decoder_input, shape=[None,None,None,n_channels])

                # Reconstruct/decode from encoding
                decoded = decoder_model(Lambda(lambda x: x)(decoder_input_wrapped)) # Lambda converts TF tensor to Keras

            # Content layer encoding for stylized out
            decoded_encoded = content_encoder_model(decoded)

        if self.mode == 'train':  # Train & summary ops only needed for training phase
            ### Losses
            with tf.name_scope('losses_'+relu_target):
                # Content loss between stylized encoding and WCT encoding
                feature_loss = feature_weight * mse(decoded_encoded, content_encoded)

                # Pixel reconstruction loss between decoded/reconstructed img and original
                pixel_loss = pixel_weight * mse(decoded, content_imgs)

                # Total Variation loss
                if tv_weight > 0:
                    tv_loss = tv_weight * tf.reduce_mean(tf.image.total_variation(decoded))
                else:
                    tv_loss = tf.constant(0.)

                total_loss = feature_loss + pixel_loss + tv_loss

            ### Training ops
            with tf.name_scope('train_'+relu_target):
                global_step = tf.Variable(0, name='global_step_train', trainable=False)
                # self.learning_rate = tf.train.exponential_decay(learning_rate, self.global_step, 100, 0.96, staircase=False)
                learning_rate = torch_decay(learning_rate, global_step, lr_decay)
                d_optimizer = tf.train.AdamOptimizer(learning_rate, beta1=0.9, beta2=0.999)

                # Only train decoder vars, encoder is frozen
                d_vars = [var for var in tf.trainable_variables() if 'decoder_'+relu_target in var.name]

                train_op = d_optimizer.minimize(total_loss, var_list=d_vars, global_step=global_step)

            ### Loss & image summaries
            with tf.name_scope('summary_'+relu_target):
                feature_loss_summary = tf.summary.scalar('feature_loss', feature_loss)
                pixel_loss_summary = tf.summary.scalar('pixel_loss', pixel_loss)
                tv_loss_summary = tf.summary.scalar('tv_loss', tv_loss)
                total_loss_summary = tf.summary.scalar('total_loss', total_loss)

                content_imgs_summary = tf.summary.image('content_imgs', content_imgs)
                decoded_images_summary = tf.summary.image('decoded_images', clip(decoded))
                
                for var in d_vars:
                    tf.summary.histogram(var.op.name, var)

                summary_op = tf.summary.merge_all()
        else:
            # For inference set unnneeded ops to None
            pixel_loss, feature_loss, tv_loss, total_loss, train_op, global_step, summary_op = [None]*7

        # Put it all together
        encoder_decoder = EncoderDecoder(content_input=content_imgs, 
                                         content_encoder_model=content_encoder_model,
                                         content_encoded=content_encoded,
                                         style_encoded=style_encoded_tensor,
                                         decoder_input=decoder_input,
                                         decoder_model=decoder_model,
                                         decoded=decoded,
                                         decoded_encoded=decoded_encoded,
                                         pixel_loss=pixel_loss,
                                         feature_loss=feature_loss,
                                         tv_loss=tv_loss,
                                         total_loss=total_loss,
                                         train_op=train_op,
                                         global_step=global_step,
                                         learning_rate=learning_rate,
                                         summary_op=summary_op)
        
        return encoder_decoder

    def build_decoder(self, input_shape, relu_target): 
        decoder_num = dict(zip(['relu1_1', 'relu2_1', 'relu3_1', 'relu4_1', 'relu5_1'], range(1,6)))[relu_target]

        decoder_archs = {
            5: [ #    layer    filts kern    HxW  / InC->OutC                                     
                (Conv2DReflect, 512, 3),  # 16x16 / 512->512
                (UpSampling2D,),          # 16x16 -> 32x32
                (Conv2DReflect, 512, 3),  # 32x32 / 512->512
                (Conv2DReflect, 512, 3),  # 32x32 / 512->512
                (Conv2DReflect, 512, 3)], # 32x32 / 512->512
            4: [
                (Conv2DReflect, 256, 3),  # 32x32 / 512->256
                (UpSampling2D,),          # 32x32 -> 64x64
                (Conv2DReflect, 256, 3),  # 64x64 / 256->256
                (Conv2DReflect, 256, 3),  # 64x64 / 256->256
                (Conv2DReflect, 256, 3)], # 64x64 / 256->256
            3: [
                (Conv2DReflect, 128, 3),  # 64x64 / 256->128
                (UpSampling2D,),          # 64x64 -> 128x128
                (Conv2DReflect, 128, 3)], # 128x128 / 128->128
            2: [
                (Conv2DReflect, 64, 3),   # 128x128 / 128->64
                (UpSampling2D,)],         # 128x128 -> 256x256
            1: [
                (Conv2DReflect, 64, 3)]   # 256x256 / 64->64
        }

        code = Input(shape=input_shape, name='decoder_input_'+relu_target)
        x = code

        ### Work backwards from deepest decoder # and build layer by layer
        decoders = reversed(range(1, decoder_num+1))

        count = 0        

        for decoder_num in decoders:
            for layer_tup in decoder_archs[decoder_num]:
                layer_name = '{}_{}'.format(relu_target, count)
                if layer_tup[0] == Conv2DReflect:
                    x = layer_tup[0](layer_name, *layer_tup[1:], padding='valid', activation='relu', name=layer_name)(x)
                elif layer_tup[0] == UpSampling2D:
                    x = layer_tup[0](name=layer_name)(x)
                count += 1

        layer_name = '{}_{}'.format(relu_target, count)
        output = Conv2DReflect(layer_name, 3, 3, padding='valid', activation=None, name=layer_name)(x)  # 256x256 / 64->3
        
        decoder_model = Model(code, output, name='decoder_model_'+relu_target)
        print(decoder_model.summary())
        return decoder_model
