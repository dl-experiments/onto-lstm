import warnings

from keras.layers import LSTM
from keras.engine import InputSpec
from keras import backend as K
from keras_extensions import changing_ndim_rnn

class OntoAttentionLSTM(LSTM):
    '''
    Modification of LSTM implementation in Keras to take a WordNet subtree instead of a the word at each timestep.
    The WordNet subtree is given as a sense separated hypernym hierarchy i.e., words are represented as tensors
    instead of vectors at each time step. The wows in the tensors are shared, as synsets are shared across words in
    WordNet. We take a weighted average of the tensor using attention mechanism conditioned on the output of the 
    previous timestep to get a vector, and that vector is processed in the same way the input is processed by LSTM.
    '''
    input_ndim = 5
    
    def __init__(self, output_dim, num_senses, num_hyps, use_attention=False, return_attention=False, **kwargs):
        # Set output_dim in kwargs so that we can pass it along to LSTM's init
        kwargs['output_dim'] = output_dim
        self.num_senses = num_senses
        self.num_hyps = num_hyps
        self.use_attention = use_attention
        self.return_attention = return_attention
        super(OntoAttentionLSTM, self).__init__(**kwargs)
        # Recurrent would have set the input shape to cause the input dim to be 3. Change it.
        self.input_spec = [InputSpec(ndim=5)]
        if self.consume_less == "cpu":
            # In the LSTM implementation in Keras, consume_less = cpu causes all gates' inputs to be precomputed
            # and stored in memory. However, this doesn't work with OntoLSTM since the input to the gates is 
            # dependent on the previous timestep's output.
            warnings.warn("OntoLSTM does not support consume_less = cpu. Changing it to mem.")
            self.consume_less = "mem"

    def build(self, input_shape):
        self.input_spec = [InputSpec(shape=input_shape)]
        input_dim = input_shape[4] - 1  # ignore sense prior parameter
        self.input_dim = input_dim
        # Saving onto-lstm weights to set them later. This way, LSTM's build method won't 
        # delete them.
        initial_ontolstm_weights = self.initial_weights
        self.initial_weights = None
        lstm_input_shape = input_shape[:2] + (input_dim,) # removing senses and hyps
        # Now calling LSTM's build to initialize the LSTM weights
        super(OntoAttentionLSTM, self).build(lstm_input_shape)
        # This would have changed the input shape and ndim. Reset it again.
        self.input_spec = [InputSpec(shape=input_shape)]

        if self.use_attention:
            # Following are the attention parameters
            self.input_hyp_projector = self.inner_init((input_dim, self.output_dim),
                name='{}_input_hyp_projector'.format(self.name)) # Projection operator for synsets
            self.context_hyp_projector = self.inner_init((self.output_dim, self.output_dim),
                name='{}_context_hyp_projector'.format(self.name)) # Projection operator for hidden state (context)
            self.hyp_scorer = self.init((self.output_dim,), name='{}_hyp_scorer'.format(self.name))

            # LSTM's build method would have initialized trainable_weights. Add to it.
            self.trainable_weights.extend([self.input_hyp_projector,
                                           self.context_hyp_projector, self.hyp_scorer])

        if initial_ontolstm_weights is not None:
            self.set_weights(initial_ontolstm_weights)
            del initial_ontolstm_weights

    def get_initial_states(self, x):
        # Reimplementing because ndim of x is 5. (samples, timesteps, num_senses, num_hyps, embedding_dim)
        sense_hyp_stripped_x = x[:, :, 0, 0, :-1]  # (samples, timesteps, input_dim), just like LSTM input.
        # We need the same initial states as regular LSTM
        return super(OntoAttentionLSTM, self).get_initial_states(sense_hyp_stripped_x)

    def _step(self, x_onto_aware, states):
        h_tm1 = states[0]
        mask_i = states[-1]  # (samples, senses, hyps, 1)
        lstm_states = states[:-1]

        # Before the step function is called, the original input is dimshuffled to have (time, samples, senses, hyps, concept_dim)
        # So shape of x_onto_aware is (samples, senses, hyps, concept_dim + 1), +1 for sense prior parameter
        # TODO: Use sense priors even when not using attention?
        x_synset_embeddings = x_onto_aware[:,:,:,:-1]  # (samples, senses, hyps, embedding_dim)

        # Sense probability calculation
        # Taking only the last dimension from all samples. These are the lambda values of exp distributions.
        sense_parameters = K.expand_dims(x_onto_aware[:, 0, 0, -1])  # (samples,1)
        # (1, num_senses)
        sense_indices = K.cast_to_floatx([[ind for ind in range(self.num_senses)]])
        # (samples, num_senses)
        expanded_sense_indices = K.dot(K.ones_like(sense_parameters), sense_indices)
        # Getting the sense probabilities from the exponential distribution. p(x) = \lambda * e^(-\lambda * x)
        sense_scores = sense_parameters * K.exp(-sense_parameters * expanded_sense_indices)  # (samples, num_senses)
        # Renormalizing sense scores to make \sum_{num_senses} p(sense | word) = 1
        if mask_i is not None:
            sense_mask = K.sum(K.squeeze(mask_i, axis=-1), axis=2)  # (samples, sense)
            sense_scores = K.switch(sense_mask, sense_scores, K.zeros_like(sense_scores))
        sense_probabilities = sense_scores / K.expand_dims(K.sum(sense_scores, axis=1) + K.epsilon())  # (samples, num_senses)
        
        if self.use_attention:
             
            # Generalization attention
            input_hyp_projection = K.dot(x_synset_embeddings, self.input_hyp_projector) # (samples, senses, hyps, proj_dim)
            context_hyp_projection = K.dot(h_tm1, self.context_hyp_projector) # (samples, proj_dim)
            context_hyp_projection_expanded = K.expand_dims(K.expand_dims(context_hyp_projection,
                                                                          dim=1),
                                                            dim=1)  #(samples, 1, 1, proj_dim)
            hyp_projection = K.sigmoid(input_hyp_projection + context_hyp_projection_expanded) # (samples, senses, hyps, proj_dim)
            hyp_scores = K.dot(hyp_projection, self.hyp_scorer) # (samples, senses, hyps)
            if mask_i is not None:
                hyp_scores = K.switch(K.squeeze(mask_i, axis=-1), hyp_scores, K.zeros_like(hyp_scores))
            scores_shape = K.shape(hyp_scores)
            # We need to flatten this because we cannot perform softmax on tensors.
            flattened_scores = K.batch_flatten(hyp_scores)  # (samples, senses*hyps)
            hyp_attention = K.reshape(K.softmax(flattened_scores), scores_shape)  # (samples, senses, hyps)
        else:
            # matrix of ones for scores to be consistent (samples, senses, hyps)
            hyp_attention = K.ones_like(x_synset_embeddings)[:, :, :, 0]
            if mask_i is not None:
                hyp_attention = K.switch(K.squeeze(mask_i, axis=-1), hyp_attention, K.zeros_like(hyp_attention))

        # Renormalizing hyp attention to get p(hyp | sense, word). Summing over hyps.
        hyp_given_sense_attention = hyp_attention / K.expand_dims(K.sum(hyp_attention, axis=2) + K.epsilon())
        # Multiply P(hyp | sense, word) and p(sense|word) . Attention values now sum to 1.
        sense_hyp_attention = hyp_given_sense_attention * K.expand_dims(sense_probabilities)

        if mask_i is not None:
            # Applying the mask on input
            zeros_like_input = K.zeros_like(x_synset_embeddings)  # (samples, senses, hyps, dim)
            x_synset_embeddings = K.switch(mask_i, x_synset_embeddings, zeros_like_input) 
            
        weighted_product = x_synset_embeddings * K.expand_dims(sense_hyp_attention)  # (samples, senses, hyps, input_dim)
        # Weighted average, summing over senses and hyps
        lstm_input_t = K.sum(weighted_product, axis=(1,2))  # (samples, input_dim)
        # Now pass the computed lstm_input to LSTM's step function to get current h and c.
        h, [_, c] = super(OntoAttentionLSTM, self).step(lstm_input_t, lstm_states)
        
        return h, c, sense_hyp_attention
        
    def step(self, x, states):
        h, c, att = self._step(x, states)
        if self.return_attention:
            return att, [h, c]
        else:
            return h, [h, c]

    def get_constants(self, x):
        # Reimplementing because ndim of x is 5. (samples, timesteps, num_senses, num_hyps, input_dim)
        sense_hyp_stripped_x = x[:, :, 0, 0, :-1]  # (samples, timesteps, input_dim), just like LSTM input.
        # We need the same constants as regular LSTM.
        lstm_constants = super(OntoAttentionLSTM, self).get_constants(sense_hyp_stripped_x)
        return lstm_constants
    
    def compute_mask(self, input, mask):
        # redefining compute mask because the input ndim is different from the output ndim, and 
        # this needs to be handled.
        if self.return_sequences and mask is not None:
            # Get rid of syn and hyp dimensions
            # TODO: Ignore sense prior?
            return K.sum(mask, axis=(-3, -2))
        else:
            return None

    def call(self, x, mask=None):
        # Overriding call to make a call to our own rnn instead of the inbuilt rnn.
        # Keras assumes we won't need access to the mask in the step function. But we do, for properly 
        # averaging x (while ignoring masked parts). Moreover, since input's ndim is not the same as 
        # output's ndim, we'll need to process the mask within rnn to define a separate output mask.
        # See the definition of changing_ndim_rnn for more details.
        input_shape = self.input_spec[0].shape
        if self.stateful:
            initial_states = self.states
        else:
            initial_states = self.get_initial_states(x)
        constants = self.get_constants(x)
        preprocessed_input = self.preprocess_input(x)

        last_output, outputs, states = changing_ndim_rnn(self.step, preprocessed_input,
                                                         initial_states,
                                                         go_backwards=self.go_backwards,
                                                         mask=mask,
                                                         constants=constants,
                                                         unroll=self.unroll,
                                                         input_length=input_shape[1],
                                                         eliminate_mask_dims=(1, 2))
        if self.stateful:
            self.updates = []
            for i in range(len(states)):
                self.updates.append((self.states[i], states[i]))

        if self.return_sequences:
            return outputs
        else:
            return last_output

    def get_config(self):
        config = {"num_senses": self.num_senses,
                  "num_hyps": self.num_hyps,
                  "use_attention": self.use_attention,
                  "return_attention": self.return_attention}
        base_config = super(OntoAttentionLSTM, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
