import tensorflow as tf

import Models.InterpolationLayer
import Utils
from Utils import LeakyReLU
import numpy as np
import Models.OutputLayer

import Models.Mhe
from Models.Mhe import add_thomson_constraint

class UnetAudioSeparator:
    '''
    U-Net separator network for singing voice separation.
    Uses valid convolutions, so it predicts for the centre part of the input - only certain input and output shapes are therefore possible (see getpadding function)
    '''

    def __init__(self, model_config):
        '''
        Initialize U-net
        :param num_layers: Number of down- and upscaling layers in the network 
        :param mhe: Indicates if MHE regularization will be applied
        :param mhe_model: Indicates MHE model to use (standard or half-space)
        :param mhe_power: Euclidean or angular distance for MHE
        '''
        self.num_layers = model_config["num_layers"]
        self.num_initial_filters = model_config["num_initial_filters"]
        self.filter_size = model_config["filter_size"]
        self.merge_filter_size = model_config["merge_filter_size"]
        self.input_filter_size = model_config["input_filter_size"]
        self.output_filter_size = model_config["output_filter_size"]
        self.upsampling = model_config["upsampling"]
        self.output_type = model_config["output_type"]
        self.context = model_config["context"]
        self.padding = "VALID" if model_config["context"] else "SAME" # requires capital letters here
        self.source_names = model_config["source_names"]
        self.num_channels = 1 if model_config["mono_downmix"] else 2
        self.output_activation = model_config["output_activation"]
        
        self.mhe = model_config["mhe"]
        self.mhe_model = model_config["mhe_model"]
        self.mhe_power = model_config["mhe_power"]

    def get_padding(self, shape):
        '''
        Calculates the required amounts of padding along each axis of the input and output, so that the Unet works and has the given shape as output shape
        :param shape: Desired output shape 
        :return: Input_shape, output_shape, where each is a list [batch_size, time_steps, channels]
        '''

        if self.context:
            # Check if desired shape is possible as output shape - go from output shape towards lowest-res feature map
            rem = float(shape[1]) # Cut off batch size number and channel

            # Output filter size
            rem = rem - self.output_filter_size + 1

            # Upsampling blocks
            for i in range(self.num_layers):
                rem = rem + self.merge_filter_size - 1
                rem = (rem + 1.) / 2.# out = in + in - 1 <=> in = (out+1)/

            # Round resulting feature map dimensions up to nearest integer
            x = np.asarray(np.ceil(rem),dtype=np.int64)
            assert(x >= 2)

            # Compute input and output shapes based on lowest-res feature map
            output_shape = x
            input_shape = x

            # Extra conv
            input_shape = input_shape + self.filter_size - 1

            # Go from centre feature map through up- and downsampling blocks
            for i in range(self.num_layers):
                output_shape = 2*output_shape - 1 #Upsampling
                output_shape = output_shape - self.merge_filter_size + 1 # Conv

                input_shape = 2*input_shape - 1 # Decimation
                if i < self.num_layers - 1:
                    input_shape = input_shape + self.filter_size - 1 # Conv
                else:
                    input_shape = input_shape + self.input_filter_size - 1

            # Output filters
            output_shape = output_shape - self.output_filter_size + 1

            input_shape = np.concatenate([[shape[0]], [input_shape], [self.num_channels]])
            output_shape = np.concatenate([[shape[0]], [output_shape], [self.num_channels]])

            return input_shape, output_shape
        else:
            return [shape[0], shape[1], self.num_channels], [shape[0], shape[1], self.num_channels]
        
            
    def get_output(self, input, training, return_spectrogram=False, reuse=True):
        '''
        Creates symbolic computation graph of the U-Net for a given input batch
        
        NOTE: the *tf.layer.conv1d* implementation was changed to *tf.nn.conv1d* in order to declare weights explicitly
        and use them for MHE regularization. Hence, the activation function has to be declared outside the convolution
        
        :param input: Input batch of mixtures, 3D tensor [batch_size, num_samples, num_channels]
        :param reuse: Whether to create new parameter variables or reuse existing ones (JPL: doesn't change output)
        :return: U-Net output: List of source estimates. Each item is a 3D tensor [batch_size, num_out_samples, num_channels]
        '''
        with tf.variable_scope("separator", reuse=reuse):
            enc_outputs = list()
            current_layer = input

            # Down-convolution: Repeat strided conv
            for i in range(self.num_layers):
                # Variable scope corresponding to each layer
                with tf.variable_scope("down_conv_"+str(i)): #, reuse=reuse):
                    
                    # 1. Define weights tensor for downsampling blocks
                    n_filt = self.num_initial_filters + (self.num_initial_filters * i) # number of filters in the current layer
                    num_in_channels = current_layer.get_shape().as_list()[-1] # get number of in channels from input data
                    shape = [self.filter_size, num_in_channels, n_filt] # should be [kernel_size, num_in_channels, num_filters]
                    W = tf.get_variable('W', shape=shape) #, initializer=tf.random_normal_initializer()) # get weights for a given layer
                    
                    # 2. Add MHE (thompson constraint) to the collection if in use
                    if self.mhe:
                        add_thomson_constraint(W, n_filt, self.mhe_model, self.mhe_power)
                    
                    # 3. Create layer using tf.nn.conv1d instead of tf.layer.conv1d: this involves applying activation outside the function
                    current_layer = tf.nn.conv1d(current_layer, W, stride=1, padding=self.padding) # out = in - filter + 1
                    current_layer = tf.nn.leaky_relu(current_layer) # Built-in Leaky ReLu with alpha=0.2 (default) as in Utils.LeakyReLu
                    enc_outputs.append(current_layer) # Append the resulting feature vector to the output
                    current_layer = current_layer[:,::2,:] # Decimate by factor of 2 # out = (in-1)/2 + 1

            # Last layer of the downsampling path to obtain features
            with tf.variable_scope("down_conv_"+str(self.num_layers)): #, reuse=reuse):
                n_filt = self.num_initial_filters + (self.num_initial_filters * self.num_layers) # number of filters in last layer 
                num_in_channels = current_layer.get_shape().as_list()[-1] # get number of in channels from input data
                shape = [self.filter_size, num_in_channels, n_filt]
                W = tf.get_variable('W', shape=shape) #, initializer=tf.random_normal_initializer()) # get weights
                
                # Add MHE (thompson constraint) to the collection if in use
                if self.mhe:
                        add_thomson_constraint(W, n_filt, self.mhe_model, self.mhe_power)
                
                # Convolution STRIDE=1 WASNT IN THE ORIGINAL U-NET. THE SAME FOR NEXT CONV
                current_layer = tf.nn.conv1d(current_layer, W, stride=1, padding=self.padding) # One more conv here since we need to compute features after last decimation
                current_layer = tf.nn.leaky_relu(current_layer) # Built-in Leaky ReLu with alpha=0.2 (default) as in Utils.LeakyReLu
                
            # Feature map here shall be X along one dimension

            # Upconvolution
            for i in range(self.num_layers):
                #UPSAMPLING
                current_layer = tf.expand_dims(current_layer, axis=1)
                if self.upsampling == 'learned':
                    # Learned interpolation between two neighbouring time positions by using a convolution filter of width 2, and inserting the responses in the middle of the two respective inputs
                    current_layer = Models.InterpolationLayer.learned_interpolation_layer(current_layer, self.padding, i)
                else:
                    if self.context:
                        current_layer = tf.image.resize_bilinear(current_layer, [1, current_layer.get_shape().as_list()[2] * 2 - 1], align_corners=True)
                    else:
                        current_layer = tf.image.resize_bilinear(current_layer, [1, current_layer.get_shape().as_list()[2]*2]) # out = in + in - 1
                current_layer = tf.squeeze(current_layer, axis=1)
                
                # UPSAMPLING FINISHED

                assert(enc_outputs[-i-1].get_shape().as_list()[1] == current_layer.get_shape().as_list()[1] or self.context) #No cropping should be necessary unless we are using context
                current_layer = Utils.crop_and_concat(enc_outputs[-i-1], current_layer, match_feature_dim=False)
                
                # Change implementation to tf.nn.conv1d to save weights and use them to calculate MHE
                with tf.variable_scope("up_conv_"+str(i)): #, reuse=reuse):
                    n_filt = self.num_initial_filters + (self.num_initial_filters * (self.num_layers - i - 1))
                    num_in_channels = current_layer.get_shape().as_list()[-1] # get number of in channels from input data
                    shape = [self.merge_filter_size, num_in_channels, n_filt] # merge_filter_size --> size of the upsampling filters
                    W = tf.get_variable('W', shape=shape) #, initializer=tf.random_normal_initializer()) # get weights
                    
                    # Add MHE (thompson constraint) to the collection when in use
                    if self.mhe:
                        add_thomson_constraint(W, n_filt, self.mhe_model, self.mhe_power)
                        
                    # De-Convolution
                    current_layer = tf.nn.conv1d(current_layer, W, stride=1, padding=self.padding)  # out = in - filter + 1
                    current_layer = tf.nn.leaky_relu(current_layer) # Built-in Leaky ReLu with alpha=0.2 (default) as in Utils.LeakyReLu
            
            # Last concatenation
            current_layer = Utils.crop_and_concat(input, current_layer, match_feature_dim=False)

            # Output layer
            # Determine output activation function
            if self.output_activation == "tanh":
                out_activation = tf.tanh
            elif self.output_activation == "linear":
                out_activation = lambda x: Utils.AudioClip(x, training)
            else:
                raise NotImplementedError

            if self.output_type == "direct":
                return Models.OutputLayer.independent_outputs(current_layer, self.source_names, self.num_channels, self.output_filter_size, self.padding, out_activation)
            elif self.output_type == "difference":
                cropped_input = Utils.crop(input,current_layer.get_shape().as_list(), match_feature_dim=False)
                #return Models.OutputLayer.difference_output(cropped_input, current_layer, self.source_names, self.num_channels, self.output_filter_size, self.padding, out_activation, training, self.mhe, self.mhe_power, reuse) # This line if MHE for Output layer is in use
                return Models.OutputLayer.difference_output(cropped_input, current_layer, self.source_names, self.num_channels, self.output_filter_size, self.padding, out_activation, training) # Use this line if MHE for Output layer is not implemented
            else:
                raise NotImplementedError