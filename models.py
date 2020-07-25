"""
models.py:  Classes for the modeling

"""
import tensorflow as tf
tfk = tf.keras
tfkm = tf.keras.models
tfkl = tf.keras.layers
import tensorflow.keras.backend as K
import numpy as np
import sklearn.preprocessing
import pickle
from tensorflow.keras.optimizers import Adam
import os

from spherical import spherical_add_sub, cart_to_sph

class PhyConst(object):
    """
    Physical constants
    """
    light_speed = 2.99792458e8
    

class DataFormat(object):
    """
    Constants for data format
    """
    
    # Indices for the angle data
    aoa_phi_ind = 0
    aoa_theta_ind = 1
    aod_phi_ind = 2
    aod_theta_ind = 3
    nangle = 4
    ang_name = ['AoA_Phi', 'AoA_theta', 'AoD_phi', 'AoD_theta']
    
    # Maximum number of paths
    npaths_max = 25
    


class CondVAE(object):
    def __init__(self, nlatent, ndat, ncond, nunits_enc=(25,10,),\
                 nunits_dec=(10,25,), out_var_min=1e-4,\
                 init_kernel_stddev=10.0, init_bias_stddev=10.0,\
                 nsort=0):
        """
        Conditional VAE class

        Parameters
        ----------
        nlatent : int
            number of latent states
        ndat : int
            number of features in the data to be modeled
        ncond : int
            number of conditional variables
        nunits_enc : list of integers
            number of hidden units in each layer of the encoder
        nunits_dec : list of integers
            number of hidden units in each layer of the decoder
        nsort : int
            Sort sthe first nsort values of the output.
            This is used for the path loss data where nsort=npaths_max
        out_var_min:  scalar
            minimum output variance.  This is used for improved conditioning
        init_kernel_stddev : scalar
            std deviation of the kernel in the initialization
        init_bias_stddev : scalar
            std deviation of the bias in the initialization

        """   
        self.nlatent = nlatent
        self.ndat = ndat        
        self.ncond = ncond
        self.nunits_enc = nunits_enc
        self.out_var_min = out_var_min
        self.nsort = nsort
        self.init_kernel_stddev = init_kernel_stddev
        self.init_bias_stddev = init_bias_stddev
        
        self.build_encoder()
        self.build_decoder()
        self.build_vae()
        

    def build_encoder(self):
        """
        Builds the encoder network
        
        The encoder maps [x,cond] to [z_mu, z_log_var]
        """
        x = tfkl.Input((self.ndat,), name='x')
        cond = tfkl.Input((self.ncond,), name='cond')
        
        dat_cond = tfkl.Concatenate(name='dat_cond')([x, cond])
        
        
        
        # Add the hidden layers
        h = dat_cond
        layer_names = []
        for i in range(len(self.nunits_enc)):           
            h = tfkl.Dense(self.nunits_enc[i], activation='sigmoid',\
                           name='FC%d' % i)(h)
            layer_names.append('FC%d' % i)
            
        # Add the final output layer                
        z_mu = tfkl.Dense(self.nlatent, activation='linear',\
                          bias_initializer=None, name='z_mu')(h)
        z_log_var = tfkl.Dense(self.nlatent, activation='linear',\
                          bias_initializer=None, name='z_log_var')(h)
                
        # Save the encoder model
        self.encoder = tfkm.Model([x, cond],\
                                  [z_mu, z_log_var])

        # Set the initialization
        set_initialization(self.encoder, layer_names,\
                           self.init_kernel_stddev, self.init_bias_stddev)        
            
    def reparm(self, z_mu, z_log_var):
        """
        Re-parametrization layer
        
            z = z_mu + eps * tf.exp(z_log_var*0.5)
            
        where eps is unit Gaussian
        """
        batch_shape = tf.shape(z_mu)
        eps = tf.random.normal(shape=batch_shape)
        z = eps * tf.exp(z_log_var*0.5) + z_mu
        return z
        
        
    def build_decoder(self):
        """
        Builds the decoder network.
        The decoder network is the generative model mapping:
            
            [z,cond] to xhat

        """   
        # Input layer
        z_samp = tfkl.Input((self.nlatent,), name='z')
        cond = tfkl.Input((self.ncond,), name='cond')   
        z_cond = tfkl.Concatenate(name='z_cond')([z_samp, cond])
        
        # Hidden layers
        layer_names = []
        h = z_cond
        for i in range(len(self.nunits_enc)):            
            h = tfkl.Dense(self.nunits_enc[i], activation='sigmoid',\
                           bias_initializer=None, name='FC%d' % i)(h)
            layer_names.append('FC%d' % i)
            
        # Add the output mean 
        x_mu = tfkl.Dense(self.ndat, name='x_mu',\
                          bias_initializer=None)(h)
        # Add sorting of the first path loss values 
        if self.nsort > 0:
            x_mu_pl = x_mu[:,:self.nsort]            
            x_mu_pl = tf.sort(x_mu_pl, direction='DESCENDING', axis=-1)
            x_mu_ang = x_mu[:,self.nsort:]
            x_mu = tfkl.Concatenate()([x_mu_pl, x_mu_ang])
                                
        # Add the output variance.                            
        x_log_var = tfkl.Dense(self.ndat, name='x_log_var')(h)   
        x_log_var = tf.maximum(x_log_var, np.log(self.out_var_min) )            
        
        # Build the decoder
        self.decoder = tfkm.Model([z_samp, cond], [x_mu, x_log_var])
        
        # Set the initialization
        set_initialization(self.decoder, layer_names,\
                           self.init_kernel_stddev, self.init_bias_stddev)      
        
        # Build the decoder with sampling
        x_samp = self.reparm(x_mu, x_log_var)
        self.sampler = tfkm.Model([z_samp, cond], x_samp)
        
        
    def build_vae(self):
        """
        Builds the VAE to train.  
        
        The VAE takes an input sample x and outputs [xhat,x_log_var].
        It also has the reconstruction and KL divergence loss
        
        """
        # Build the encoder and decoder
        self.build_encoder()
        self.build_decoder()
        
        # Inputs for the VAE
        x = tfkl.Input((self.ndat,), name='x')
        cond = tfkl.Input((self.ncond,), name='cond')
        
        # Encoder
        z_mu, z_log_var = self.encoder([x,cond])
        z_samp = self.reparm(z_mu, z_log_var)
        
        # Decoder
        xhat, x_log_var = self.decoder([z_samp, cond])
        self.vae = tfkm.Model([x, cond], [xhat, x_log_var])
            
        # Add reconstruction loss   
        recon_loss = K.square(xhat - x)*tf.exp(-x_log_var) + x_log_var + \
            np.log(2*np.pi)
        recon_loss = 0.5*K.sum(recon_loss, axis=-1)
        
        # Add the KL divergence loss
        kl_loss = 1 + z_log_var - K.square(z_mu) - K.exp(z_log_var)
        kl_loss = K.sum(kl_loss, axis=-1)
        kl_loss *= -0.5
        vae_loss = K.mean(recon_loss + kl_loss)
        self.vae.add_loss(vae_loss)

class ChanMod(object):
    """
    Object for modeling mmWave channel model data.
    
    There are two parts in the model:
        * link_mod:  This predicts the link_state (i.e. LOS, NLOS or no link)
          from the link conditions.  This is implemented a neural network
        * path_mod:  This predicts the other channel parameters (right now,
          this is the vector of path losses) from the condition and link_state.
        
    Each model has a pre-processor on the data and conditions that is also
    trained.
          
    """
    
    """
    Static variables
    """
    # Link states
    no_link = 0
    los_link = 1
    nlos_link = 2
    nlink_states = 3
    
    # Cell types
    terr_cell = 0
    aerial_cell = 1
    ncell_type = 2
    
    # Numbers of transformed features for models
    nin_link = 5   # num features for link predictor model
    ncond = 5      # num condition features for path model
        
    def __init__(self,npaths_max=25,pl_max=200, nlatent=10,\
                 nunits_enc=(50,20), nunits_dec=(20,50), \
                 nunits_link=(25,10), add_zero_los_frac=0.25,out_var_min=1e-4,\
                 init_bias_stddev=10.0, init_kernel_stddev=10.0,\
                 model_dir='model_data', fc=28e9):
        """
        Constructor

        Parameters
        ----------
        npaths_max : int
            max number of paths per link
        pl_max : scalar
            max path loss in dB
        nunits_enc : list of integers
            number of hidden units in each layer of the encoder
        nunits_dec : list of integers
            number of hidden units in each layer of the decoder
        nunits_link:  list of integers
            number of hidden units in each layer of the link classifier
        nlatent : int
            number of latent states in the VAE model
        nunits_enc : list of integers
            number of hidden units in each layer of the encoder
        nunits_dec : list of integers
            number of hidden units in each layer of the decoder  
        add_zero_los_frac: scalar
            in the link state modeling, a fraction of points at the origin
            are added to ensure the model predicts a LOS link there.
        out_var_min:  scalar
            minimum output variance.  This is used for improved conditioning 
        init_kernel_stddev : scalar
            std deviation of the kernel in the initialization
        init_bias_stddev : scalar
            std deviation of the bias in the initialization
        model_dir : string
            path to the directory for all the model files.
            if this path does not exist, it will be created 
        fc : scalar
            carrier frequency in Hz            
        """
        self.npaths_max = npaths_max
        self.pl_max = pl_max
        self.ndim = 3  # number of spatial dimensions
        self.nunits_link = nunits_link
        self.init_kernel_stddev = init_kernel_stddev
        self.init_bias_stddev = init_bias_stddev
        self.model_dir = model_dir
        
        self.nlatent = nlatent
        self.nunits_enc = nunits_enc
        self.nunits_dec = nunits_dec
        self.add_zero_los_frac = add_zero_los_frac
        self.out_var_min = out_var_min        
        self.fc = fc
        
        
    
    def transform_link(self,dvec,cell_type,fit=False):
        """
        Pre-processes input for the link classifier network

        Parameters
        ----------
        dvec : (nlink,3) array
            vector from cell to UAV
        cell_type : (nlink,) array of ints
            cell type.  One of terr_cell, aerial_cell

        Returns
        -------
        X:  (nlink,nin_link) array:
            transformed data for input to the NN
        """
        
        # 3D distance and vertical distance.
        # Note that vertical distance can be negative
        #dx = np.sqrt(np.sum(dvec[:,0]**2, axis=1))
        dx = np.sqrt(dvec[:,0]**2 + dvec[:,1]**2)
        dz = dvec[:,2]
        
        X0 = np.column_stack((dx, dz, dx*cell_type, dz*cell_type, cell_type))
                    
        
        # Transform the data with the scaler.
        # If fit is set, the transform is also learned
        if fit:
            self.link_scaler = sklearn.preprocessing.StandardScaler()
            X = self.link_scaler.fit_transform(X0)
        else:
            X = self.link_scaler.transform(X0)
        return X
        
        
    def build_link_mod(self):
        """
        Builds the link classifier neural network            
        """              
        
        # Input layer
        self.link_mod = tfkm.Sequential()
        self.link_mod.add(tfkl.Input(self.nin_link, name='Input'))
        
        # Hidden layers
        for i, nh in enumerate(self.nunits_link):
            self.link_mod.add(tfkl.Dense(nh, activation='sigmoid', name='FC%d' % i))
        
        # Output softmax for classification
        self.link_mod.add(tfkl.Dense(self.nlink_states, activation='softmax', name='Output'))
              
    
    def add_los_zero(self,dvec,cell_type,ls):
        """
        Appends points at dvec=0 with LOS.  This is used to 
        ensure the model predicts a LOS link at zero distance.

        Parameters
        ----------
        dvec : (nlink,ndim) array
            vector from cell to UAV
        cell_type : (nlink,) array of ints
            cell type. 
        ls : (nlink,) array of ints
            link types

        Returns
        -------
        dvec, cell_type, ls : as above
            Values with the zeros appended at the end

        """
        
        ns = dvec.shape[0]
        nadd = int(ns*self.add_zero_los_frac)
        if nadd <= 0:
            return dvec, cell_type, ls
        
        I = np.random.randint(ns,size=(nadd,))
        
        # Variables to append
        cell_type1 = cell_type[I]
        ls1 = np.tile(ChanMod.los_link, nadd)
        dvec1 = np.zeros((nadd,3))
        
        # Add the points
        cell_type = np.hstack((cell_type, cell_type1))
        ls = np.hstack((ls, ls1))
        dvec = np.vstack((dvec, dvec1))
        return dvec, cell_type, ls
        
        
        
    
    def fit_link_mod(self, train_data, test_data, epochs=50, lr=1e-4):
        """
        Trains the link classifier model

        Parameters
        ----------
        train_data : dictionary
            training data dictionary.
        test_data : dictionary
            test data dictionary.    
        """      
        
        
        # Get the link state
        ytr = get_link_state(train_data['los_exists'], train_data['nlos_pl'],\
                             self.pl_max)
        yts = get_link_state(test_data['los_exists'], test_data['nlos_pl'],\
                             self.pl_max)
        
        
        # Get the position and cell types
        dvectr = train_data['dvec']
        celltr = train_data['cell_type']
        dvects = test_data['dvec']
        cellts = test_data['cell_type']
        
        # Fit the transforms
        self.transform_link(dvectr,celltr, fit=True)

        # Append the zero points        
        dvectr, celltr, ytr = self.add_los_zero(dvectr,celltr,ytr)
        dvects, cellts, yts = self.add_los_zero(dvects,cellts,yts)
                        
        # Transform the input to the neural network
        Xtr = self.transform_link(dvectr,celltr)
        Xts = self.transform_link(dvects,cellts)
                    
        # Fit the neural network
        opt = Adam(lr=lr)
        self.link_mod.compile(opt,loss='sparse_categorical_crossentropy',\
                metrics=['accuracy'])
        
        self.link_hist = self.link_mod.fit(\
                Xtr,ytr, batch_size=100, epochs=epochs, validation_data=(Xts,yts) )            
            
    
    def link_predict(self,dvec,cell_type):
        """
        Predicts the link state

        Parameters
        ----------
        dvec : (nlink,ndim) array
            vector from cell to UAV
        cell_type : (nlink,) array of ints
            cell type.  0 = terrestrial, 1=aerial

        Returns
        -------
        prob:  (nlink,nlink_states) array:
            probabilities of each link state

        """
        X = self.transform_link(dvec, cell_type)
        prob = self.link_mod.predict(X)
        return prob
    
    def save_link_model(self, weights_fn='link_weights.h5', preproc_fn='link_preproc.p'):
        """
        Saves link state predictor model data to files

        Parameters
        ----------
        weights_fn : string
            Filename for the link neural network weights.  This is an H5 file
        preproc_fn : string
            Filename for the pickle copy of the pre-processors

        """
        # Create the file paths
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        preproc_path = os.path.join(self.model_dir, preproc_fn)
        weigths_path = os.path.join(self.model_dir, weights_fn)
        
        
        # Save the pre-processors
        with open(preproc_path,'wb') as fp:
            pickle.dump([self.link_scaler, self.nunits_link], fp)
            
        # Save the VAE weights
        self.link_mod.save_weights(weigths_path, save_format='h5')
        
    def load_link_model(self, weights_fn='link_weights.h5', preproc_fn='link_preproc.p'):
        """
        Load link state predictor model data from files

        Parameters
        ----------
        weights_fn : string
            Filename for the VAE weights.  This is an H5 file
        preproc_fn : string
            Filename for the pickle copy of the pre-processors

        """
        # Create the file paths
        preproc_path = os.path.join(self.model_dir, preproc_fn)
        weigths_path = os.path.join(self.model_dir, weights_fn)

        # Load the pre-processors and model config
        with open(preproc_path,'rb') as fp:
            self.link_scaler, self.nunits_link = pickle.load(fp)
            
        # Build the link state predictor
        self.build_link_mod()
        
        # Load the VAE weights
        self.link_mod.load_weights(weigths_path)
        
    def build_path_mod(self):
        """
        Builds the VAE for the NLOS paths
        """
        
        # Number of data inputs in the transformed domain
        # For each sample and each path, there is:
        # * one path loss value
        # * nangle angles
        # * one delay
        # for a total of (2+nangle)*npaths_max parameters
        self.ndat = self.npaths_max*(2+DataFormat.nangle)
        
        self.path_mod = CondVAE(\
            nlatent=self.nlatent, ndat=self.ndat, ncond=ChanMod.ncond,\
            nunits_enc=self.nunits_enc, nunits_dec=self.nunits_dec,\
            out_var_min=self.out_var_min, nsort=self.npaths_max,\
            init_bias_stddev=self.init_bias_stddev,\
            init_kernel_stddev=self.init_kernel_stddev)
            
    def fit_path_mod(self, train_data, test_data, epochs=50, lr=1e-3,\
                     checkpoint_period = 0):
        """
        Trains the path model

        Parameters
        ----------
        train_data : dictionary
            training data dictionary.
        test_data : dictionary
            test data dictionary. 
        epochs: int
            number of training epochs
        lr: scalar
            learning rate
        checkpoint_period:  int
            period in epochs for saving the model checkpoints.  
            A value of 0 indicates that checkpoints are not be saved.
        """      
        # Get the link state
        ls_tr = get_link_state(train_data['los_exists'], train_data['nlos_pl'],\
                               self.pl_max)
        ls_ts = get_link_state(test_data['los_exists'], test_data['nlos_pl'],\
                               self.pl_max)
        los_tr = ls_tr == ChanMod.los_link
        los_ts = ls_tr == ChanMod.los_link
        
        
        # Extract the links that are in LOS or NLOS
        Itr = np.where(ls_tr != ChanMod.no_link)[0]
        Its = np.where(ls_ts != ChanMod.no_link)[0]
        
        # Fit and transform the condition data
        Utr = self.transform_cond(\
            train_data['dvec'][Itr], train_data['cell_type'][Itr],\
            los_tr[Itr], fit=True)
        Uts = self.transform_cond(\
            test_data['dvec'][Its], test_data['cell_type'][Its],\
            los_ts[Its])            
        
        # Fit and transform the data
        Xtr = self.transform_data(\
            train_data['dvec'][Itr],\
            train_data['nlos_pl'][Itr,:self.npaths_max],\
            train_data['nlos_ang'][Itr,:self.npaths_max,:],\
            train_data['nlos_dly'][Itr,:self.npaths_max], fit=True)
        Xts  = self.transform_data(\
            test_data['dvec'][Its],\
            test_data['nlos_pl'][Its,:self.npaths_max],\
            test_data['nlos_ang'][Its,:self.npaths_max,:],\
            test_data['nlos_dly'][Its,:self.npaths_max])
        
        # Create the checkpoint callback
        batch_size = 100
        if (checkpoint_period > 0):            
            save_freq = checkpoint_period*int(np.ceil(Xtr.shape[0]/batch_size))
            if not os.path.exists(self.model_dir):
                os.makedirs(self.model_dir)
            cp_path = os.path.join(self.model_dir, 'path_weights.{epoch:03d}.h5')
            callbacks = [tf.keras.callbacks.ModelCheckpoint(\
                filepath=cp_path, save_weights_only=True,save_freq=save_freq)]
        else:
            callbacks = []
        
        
        # Fit the model
        opt = Adam(lr=lr)
        self.path_mod.vae.compile(opt)
            
        self.path_hist = self.path_mod.vae.fit(\
                    [Xtr,Utr], batch_size=batch_size, epochs=epochs,\
                    validation_data=([Xts,Uts],None),\
                    callbacks=callbacks)
        
        # Save the history
        hist_path = os.path.join(self.model_dir, 'path_train_hist.p')        
        with open(hist_path,'wb') as fp:
            pickle.dump(self.path_hist.history, fp)
            
    def transform_dly(self, dvec, nlos_dly, fit=False):
        """
        Performs the transformation on the delay data

        Parameters
        ----------
        dvec : (nlink,ndim) array, ndim=3
            Vectors from cell to UAV for each link
        nlos_dly : (nlink,npaths_max) array 
            Absolute delay of each path in each link  
        fit:  boolean
            Indicates if transform is to be fit

        Returns
        -------
        Xdly : (nlink,npaths_max)
            Tranformed delay coordinates

        """            
        
        # Compute LOS delay
        dist = np.sqrt(np.sum(dvec**2,axis=1))        
        los_dly = dist/PhyConst.light_speed
        
        # Compute delay relative to LOS delay
        dly0 = np.maximum(0, nlos_dly - los_dly[:,None])
                
        # Transform the data with the scaler
        # If fit is set, the transform is also learned
        if fit:            
            self.dly_scale = np.mean(dly0)
        Xdly = dly0 / self.dly_scale
            
        return Xdly
    
    def inv_transform_dly(self, dvec, Xdly):
        """
        Performs the inverse transformation on the delay data

        Parameters
        ----------
        dvec : (nlink,ndim) array, ndim=3
            Vectors from cell to UAV for each link
        Xdly : (nlink,npaths_max)
            Tranformed delay coordinates

        Returns
        -------            
        nlos_dly : (nlink,npaths_max) array 
            Absolute delay of each path in each link  
        """            
        
        # Compute LOS delay
        dist = np.sqrt(np.sum(dvec**2,axis=1))        
        los_dly = dist/PhyConst.light_speed
        

        # Inverse the transform
        dly0 = Xdly * self.dly_scale
        
        # Compute the absolute delay
        nlos_dly = dly0 + los_dly[:,None]
               
        return nlos_dly
    
            
            
    def transform_ang(self, dvec, nlos_ang, nlos_pl):
        """
        Performs the transformation on the angle data

        Parameters
        ----------
        dvec : (nlink,ndim) array
            Vectors from cell to UAV for each link
        nlos_ang : (nlink,npaths_max,nangle) array
            Angles of each path in each link.  
            The angles are in degrees
        nlos_pl : (nlink,npaths_max) array 
            Path losses of each path in each link.
            A value of pl_max indicates no path

        Returns
        -------
        Xang : (nlink,nangle*npaths_max)
            Tranformed angle coordinates

        """
                
        # Compute the LOS angles
        r, los_aod_phi, los_aod_theta = cart_to_sph(dvec)
        r, los_aoa_phi, los_aoa_theta = cart_to_sph(-dvec)
        
        # Get the NLOS angles
        nlos_aod_phi   = nlos_ang[:,:,DataFormat.aod_phi_ind]
        nlos_aod_theta = nlos_ang[:,:,DataFormat.aod_theta_ind]
        nlos_aoa_phi   = nlos_ang[:,:,DataFormat.aoa_phi_ind]
        nlos_aoa_theta = nlos_ang[:,:,DataFormat.aoa_theta_ind]
        
        # Rotate the NLOS angles by the LOS angles to compute
        # the relative angle        
        aod_phi_rel, aod_theta_rel = spherical_add_sub(\
            nlos_aod_phi, nlos_aod_theta,\
            los_aod_phi[:,None], los_aod_theta[:,None])
        aoa_phi_rel, aoa_theta_rel = spherical_add_sub(\
            nlos_aoa_phi, nlos_aoa_theta,\
            los_aoa_phi[:,None], los_aoa_theta[:,None])            
            
        # Set the relative angle on non-existent paths to zero
        I = (nlos_pl < self.pl_max-0.01)
        aod_phi_rel = aod_phi_rel*I
        aod_theta_rel = aod_theta_rel*I
        aoa_phi_rel = aoa_phi_rel*I
        aoa_theta_rel = aoa_theta_rel*I
                                        
        # Stack the relative angles and scale by 180
        Xang = np.hstack(\
            (aoa_phi_rel/180, aoa_theta_rel/180,\
             aod_phi_rel/180, aod_theta_rel/180))
        
        return Xang
    
    def inv_transform_ang(self, dvec, Xang):
        """
        Performs the transformation on the angle data

        Parameters
        ----------
        dvec : (nlink,ndim) array
            Vectors from cell to UAV for each link
        Xang : (nlink,nangle*npaths_max)
            Tranformed angle coordinates            
   

        Returns
        -------
        nlos_ang : (nlink,npaths_max,nangle) array
            Angles of each path in each link.  
            The angles are in degrees        
        """
                
        # Compute the LOS angles
        r, los_aod_phi, los_aod_theta = cart_to_sph(dvec)
        r, los_aoa_phi, los_aoa_theta = cart_to_sph(-dvec)
        
        # Get the transformed angles
        npm = self.npaths_max
        aoa_phi_rel   = Xang[:,:npm]*180
        aoa_theta_rel = Xang[:,npm:2*npm]*180        
        aod_phi_rel   = Xang[:,2*npm:3*npm]*180
        aod_theta_rel = Xang[:,3*npm:]*180        
                
        # Rotate the relative angles by the LOS angles to compute
        # the original NLOS angles
        nlos_aoa_phi, nlos_aoa_theta = spherical_add_sub(\
            aoa_phi_rel, aoa_theta_rel,\
            los_aoa_phi[:,None], los_aoa_theta[:,None], sub=False)
        nlos_aod_phi, nlos_aod_theta = spherical_add_sub(\
            aod_phi_rel, aod_theta_rel,\
            los_aod_phi[:,None], los_aod_theta[:,None], sub=False)
            
        # Stack the relative angles     
        nlink = nlos_aod_phi.shape[0]
        nlos_ang = np.zeros((nlink,self.npaths_max,DataFormat.nangle))
        nlos_ang[:,:,DataFormat.aoa_phi_ind] = nlos_aoa_phi
        nlos_ang[:,:,DataFormat.aoa_theta_ind] = nlos_aoa_theta
        nlos_ang[:,:,DataFormat.aod_phi_ind] = nlos_aod_phi
        nlos_ang[:,:,DataFormat.aod_theta_ind] = nlos_aod_theta
        
        return nlos_ang
                    
        
    def transform_cond(self, dvec, cell_type, los, fit=False):
        """
        Pre-processing transform on the condition

        Parameters
        ----------
        dvec : (nlink,ndim) array
            vector from cell to UAV
        cell_type : (nlink,) array of ints
            cell type.  One of terr_cell, aerial_cell
        los:  (nlink,) array of booleans
            indicates if link is in LOS or not
        fit : boolean
            flag indicating if the transform should be fit

        Returns
        -------
        U : (nlink,ncond) array
            Transform conditioned features
        """
      
        
        # 3D distance and vertical distance.
        # Note that vertical distance can be negative
        d3d = np.maximum(np.sqrt(np.sum(dvec**2, axis=1)), 1)
        dvert = dvec[:,2]
        
        # Transform the condition variables
        U0 = np.column_stack((d3d, np.log10(d3d), dvert, cell_type, los))
        self.ncond = U0.shape[1]
        
        # Transform the data with the scaler.
        # If fit is set, the transform is also learned
        if fit:
            self.cond_scaler = sklearn.preprocessing.StandardScaler()
            U = self.cond_scaler.fit_transform(U0)
        else:
            U = self.cond_scaler.transform(U0)
            
        return U
      
    def transform_pl(self, nlos_pl, fit=False):
        """
        Transform on the NLOS path loss

        Parameters
        ----------
        pl : (nlink,npaths_max) array 
            path losses of each NLOS path in each link.
            A value of pl_max indicates no path
        fit : boolean
            flag indicating if the transform should be fit            

        Returns
        -------
        Xpl : (nlink,npaths_max) array
            Transform data features
        """
        
        # Compute the path loss below the maximum path loss.
        # Hence a value of 0 corresponds to a maximum path loss value
        X0 = self.pl_max - nlos_pl[:,:self.npaths_max]     
        
        # Transform the data with the scaler.
        # If fit is set, the transform is also learned
        if fit:
            self.pl_scaler = sklearn.preprocessing.MinMaxScaler()
            Xpl = self.pl_scaler.fit_transform(X0)
        else:
            Xpl = self.pl_scaler.transform(X0)
        return Xpl
    
    def inv_transform_pl(self, Xpl):
        """
        Inverts the transform on the NLOS path loss data

        Parameters
        ----------
        Xpl : (nlink,ndat) array 
            Transformed path loss values

        Returns
        -------
        nlos_pl : (nlink,npaths_max) array 
            Path losses of each NLOS path in each link.
            A value of pl_max indicates no path
        """
        
        # Invert the scaler
        Xpl = np.maximum(0,Xpl)
        Xpl = np.minimum(1,Xpl)
        X0 = self.pl_scaler.inverse_transform(Xpl)
        
        # Sort and make positive
        X0 = np.maximum(0, X0)
        X0 = np.fliplr(np.sort(X0, axis=-1))
        
        # Transform the condition variables
        nlos_pl = self.pl_max - X0  
                
        return nlos_pl        
        
    def transform_data(self, dvec, nlos_pl, nlos_ang, nlos_dly, fit=False):
        """
        Pre-processing transform on the data

        Parameters
        ----------
        dvec : (nlink,ndim) array
            vector from cell to UAV
        nlos_pl : (nlink,npaths_max) array 
            Path losses of each path in each link.
            A value of pl_max indicates no path
        nlos_ang : (nlink,npaths_max,nangle) array
            Angles of each path in each link.  
            The angles are in degrees           
        nlos_dly : (nlink,npaths_max) array 
            Absolute delay of each path (in seconds)
        fit : boolean
            flag indicating if the transform should be fit            

        Returns
        -------
        X : (nlink,ndat) array
            Transform data features
        """
        
        # Transform the path loss data
        Xpl = self.transform_pl(nlos_pl,fit)
        
        # Transform the angles
        Xang = self.transform_ang(dvec,nlos_ang,nlos_pl)
        
        # Transform the delays
        Xdly = self.transform_dly(dvec, nlos_dly, fit)
        
        # Concatenate
        X = np.hstack((Xpl, Xang, Xdly))
        return X
    
    def inv_transform_data(self, dvec, X):
        """
        Inverts the pre-processing transform on the data

        Parameters
        ----------
        dvec : (nlink,ndim) array
            vector from cell to UAV
        X : (nlink,ndat) array 
            Transform data features

        Returns
        -------
        nlos_pl : (nlink,npaths_max) array 
            Path losses of each path in each link.
            A value of pl_max indicates no path
        nlos_ang : (nlink,npaths_max,nangle) array
            Angles of each path in each link.  
            The angles are in degrees
        nlos_dly : (nlink,npaths_max) array 
            Absolute delay of each path (in seconds)            
        """
        
        # Split
        Xpl = X[:,:self.npaths_max]
        Xang = X[:,self.npaths_max:self.npaths_max*(DataFormat.nangle+1)]
        Xdly = X[:,self.npaths_max*(DataFormat.nangle+1):]
        
        # Invert the transforms
        nlos_pl = self.inv_transform_pl(Xpl)
        nlos_ang = self.inv_transform_ang(dvec, Xang)
        nlos_dly = self.inv_transform_dly(dvec, Xdly)
        
                
        return nlos_pl, nlos_ang, nlos_dly
    
    def get_los_path(self, dvec):
        """
        Computes LOS path loss and angles

        Parameters
        ----------
        dvec : (n,3) array            
            Vector from cell to UAV
            
        Returns
        -------
        los_pl:  (n,) array
            LOS path losses computed from Friis' Law
        los_ang:  (n,DataFormat.nangle) = (n,4) array
            LOS angles 
        los_dly:  (n,) array
            Delay of the paths computed from the speed of light
        """
        # Compute free space path loss from Friis' law
        dist = np.maximum(np.sqrt(np.sum(dvec**2,axis=1)), 1)        
        lam = 3e8/self.fc
        los_pl = 20*np.log10(dist*4*np.pi/lam)
        
        # Compute the LOS angles
        r, los_aod_phi, los_aod_theta = cart_to_sph(dvec)
        r, los_aoa_phi, los_aoa_theta = cart_to_sph(-dvec)
        
        # Stack the angles
        los_ang = np.stack((los_aoa_phi, los_aoa_theta,\
                            los_aod_phi, los_aod_theta), axis=-1)
            
        # Compute the delay
        los_dly = dist/PhyConst.light_speed
    
        return los_pl, los_ang, los_dly
        
    
    def sample_path(self, dvec, cell_type, link_state=None, nlos_only=False):
        """
        Generates random samples of the path data using the trained model

        Parameters
        ----------
        dvec : (nlink,ndim) array
            Vector from cell to UAV
        cell_type : (nlink,) array of ints
            Cell type.  One of terr_cell, aerial_cell
        link_state:  (nlink,) array of {no_link, los_link, nlos_link}            
            A value of `None` indicates that the link state should be
            generated randomly from the link state predictor model
        nlos_only: Boolean
            If `True`, returns only the NLOS path data.
            If `False`, returns the LOS and NLOS path data.
   
        Returns
        -------
        pl : (nlink,npaths_max) array 
            Path losses of each path in each link.
            A value of pl_max indicates no path
        ang: (nlink,npaths_max,DataFormat.nangle) array
            Angles of each pathin each link
        dly : (nlink,npaths_max) array 
            Absolute delay of each path in each link in seconds.           
        """
        # Get dimensions
        nlink = dvec.shape[0]

        # Generate random link states if needed
        # Use the link state predictor network
        if link_state is None:
            prob = self.link_predict(dvec, cell_type) 
            cdf = np.cumsum(prob, axis=1)            
            link_state = np.zeros(nlink)
            u = np.random.uniform(0,1,nlink)
            for i in range(cdf.shape[1]-1):
                I = np.where(u>cdf[:,i])[0]
                link_state[I] = i+1
                
        # Find the indices where there are some link
        # and where there is a LOS link
        Ilink = np.where(link_state != ChanMod.no_link)[0]
        Ilos  = np.where(link_state == ChanMod.los_link)[0]
        los   = link_state == ChanMod.los_link        
        
        # Get the condition variables and random noise
        U = self.transform_cond(dvec[Ilink], cell_type[Ilink], los[Ilink])
        nlink1 = U.shape[0]
        Z = np.random.normal(0,1,(nlink1,self.nlatent))
        
        # Run through the sampling network
        X = self.path_mod.sampler.predict([Z,U]) 
        
        # Compute the inverse transform to get back the path loss
        # and angle data
        nlos_pl, nlos_ang , nlos_dly = self.inv_transform_data(dvec[Ilink], X)
        
        # Create arrays for the PL and angles
        pl  = np.tile(self.pl_max, (nlink,self.npaths_max))
        ang = np.zeros((nlink,self.npaths_max,DataFormat.nangle))
        dly  = np.tile(self.pl_max, (nlink,self.npaths_max))
        
        # Place the NLOS data in the arrays 
        pl[Ilink]  = nlos_pl
        ang[Ilink] = nlos_ang
        dly[Ilink]  = nlos_dly
        
        if not nlos_only:
        
            # Compute the PL and angles for the LOS paths
            los_pl, los_ang, los_dly = self.get_los_path(dvec[Ilos])
            
            # Add the path loss and angles to the NLOS paths
            # On the links with LOS paths, move over the
            # the NLOS data and insert the NLOS paths
            pl[Ilos,1:] = pl[Ilos,:-1]
            ang[Ilos,1:,:] = ang[Ilos,:-1,:]
            dly[Ilos,1:] = dly[Ilos,:-1]
            pl[Ilos,0] = los_pl
            ang[Ilos,0,:] = los_ang
            dly[Ilos,0] = los_dly
            
        return pl, ang, dly
            
    
    def save_path_model(self, weights_fn='path_weights.h5', preproc_fn='path_preproc.p'):
        """
        Saves model data to files

        Parameters
        ----------
        weights_fn : string
            Filename for the VAE weights.  This is an H5 file
        preproc_fn : string
            Filename for the pickle copy of the pre-processors

        """
        # Create the file paths
        if not os.path.exists(self.model_dir):
            os.makedirs(self.model_dir)
        preproc_path = os.path.join(self.model_dir, preproc_fn)
        weigths_path = os.path.join(self.model_dir, weights_fn)
        
        # Save the pre-processors
        with open(preproc_path,'wb') as fp:
            pickle.dump([self.dat_scaler, self.cond_scaler, self.dly_scale,\
                         self.pl_max, self.npaths_max, self.nlatent,\
                         self.nunits_enc, self.nunits_dec], fp)
            
        # Save the VAE weights
        self.path_mod.vae.save_weights(weigths_path, save_format='h5')
        
    def load_path_model(self, weights_fn='path_weights.h5', preproc_fn='path_preproc.p'):
        """
        Load model data from files

        Parameters
        ----------
        weights_fn : string
            Filename for the VAE weights.  This is an H5 file
        preproc_fn : string
            Filename for the pickle copy of the pre-processors

        """
        # Create the file paths
        preproc_path = os.path.join(self.model_dir, preproc_fn)
        weights_path = os.path.join(self.model_dir, weights_fn)
        
        # Load the pre-processors
        with open(preproc_path,'rb') as fp:
            self.pl_scaler, self.cond_scaler, self.dly_scale, self.pl_max,\
                self.npaths_max, self.nlatent, self.nunits_enc,\
                self.nunits_dec = pickle.load(fp)
            
        # Build the path model
        self.build_path_mod()
            
        # Load the VAE weights
        self.path_mod.vae.load_weights(weights_path)
        
def set_initialization(mod, layer_names, kernel_stddev=1.0, bias_stddev=1.0):
    """
    Sets the bias and kernel initializations for a set of dense layers

    Parameters
    ----------
    mod:  Tensorflow model
        Model for which the initialization is to be applied
    layer_names : list of strings
        List of names of layers to apply the initialization
    kernel_stddev : scalar
        std deviation of the kernel in the initialization
    bias_stddev : scalar
        std deviation of the bias in the initialization            
    """
    for name in layer_names:
        layer = mod.get_layer(name)
        nin = layer.input_shape[-1]
        nout = layer.output_shape[-1]
        W = np.random.normal(0,kernel_stddev/np.sqrt(nin),\
                             (nin,nout)).astype(np.float32)
        b = np.random.normal(0,bias_stddev,\
                             (nout,)).astype(np.float32)
        layer.set_weights([W,b])
        
def combine_los_nlos(nlos_pl, nlos_ang, nlos_dly,\
                     los_exists, los_pl, los_ang, los_dly):
    """
    Combines LOS and NLOS path loss data

    Parameters
    ----------    
    nlos_pl : (nlink,npaths_max) array
        NLOS path losses for each path in dB
    nlos_ang : (nlink,npaths_max,nangle) array 
        Set of angles of each path in degrees
    nlos_dly : (nlink,npaths_max) array
        NLOS absolute delays of each path in seconds
    los_exists : (nlink,) array of booleans
        For each link, indicates if there is a LOS path.
        The LOS paths will be added only on these links
    los_pl : (nlink,) array
        LOS path losses for each link in dB.  
    los_ang : (nlink,npaths_max,) array 
        Set of LOS angles of each link
    los_dly : (nlink,) array
        LOS absolute delays of each path in seconds
        

    Returns
    -------
    pl : (nlink,npaths_max) array
        Combined path losses for each path in dB
    ang : (nlink,npaths_max,nangle) array 
        Combined of angles of each path in degrees
    dly : (nlink,npaths_max) array
        Combined absolute delays of each path in seconds
    """
    
   
    # Find the links with LOS paths
    Ilos = np.where(los_exists)[0]
    
    # Copy the NLOS path losses and angles
    pl = np.copy(nlos_pl)
    ang = np.copy(nlos_ang)
    dly = np.copy(nlos_dly)
    
    # On the links with LOS paths, move over the
    # the NLOS data and insert the NLOS paths
    pl[Ilos,1:] = pl[Ilos,:-1]
    ang[Ilos,1:,:] = ang[Ilos,:-1,:]
    pl[Ilos,0] = los_pl[Ilos]
    ang[Ilos,0,:] = los_ang[Ilos,:]
    dly[Ilos,0] = los_dly[Ilos]
    dly[Ilos,0,:] = los_dly[Ilos,:]
    
        
    return pl, ang
        
        
        
def get_link_state(los_exists, nlos_pl, pl_max):
    """
    Computes the link state

    Parameters
    ----------
    los_exists : (nlink,) array of boolean
        indicates if each link has an LOS path or not
    nlos_pl : (nlink,npaths_max) array of floats
        path loss for each path in the link
    pl_max : scalar
        Maximum path loss.  Values close to this value
        are considered non-existent

    Returns
    -------
    link_state : (nlink,) array of int
        indicates link state: no_link, los_link, nlos_link            
    """
    
    # Compute number of paths for each link
    npath = np.sum((nlos_pl < pl_max-0.1), axis=1)
    
    # Compute link state
    Ilos  = (los_exists==1)
    Ino   = (los_exists==0) & (npath==0)
    Inlos = (los_exists==0) & (npath>0)
    
    link_state = ChanMod.los_link*Ilos + ChanMod.nlos_link*Inlos\
        + ChanMod.no_link*Ino
    return link_state    