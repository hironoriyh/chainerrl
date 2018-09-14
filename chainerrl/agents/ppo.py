from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()  # NOQA

import copy

import chainer
from chainer import cuda
import chainer.functions as F
import numpy as np

from chainerrl import agent
from chainerrl.misc.batch_states import batch_states


def _elementwise_clip(x, x_min, x_max):
    """Elementwise clipping

    Note: chainer.functions.clip supports clipping to constant intervals
    """
    return F.minimum(F.maximum(x, x_min), x_max)


class PPO(agent.AttributeSavingMixin, agent.Agent):
    """Proximal Policy Optimization

    See https://arxiv.org/abs/1707.06347

    Args:
        model (A3CModel): Model to train.  Recurrent models are not supported.
            state s  |->  (pi(s, _), v(s))
        optimizer (chainer.Optimizer): Optimizer used to train the model
        gpu (int): GPU device id if not None nor negative
        gamma (float): Discount factor [0, 1]
        lambd (float): Lambda-return factor [0, 1]
        phi (callable): Feature extractor function
        value_func_coef (float): Weight coefficient for loss of
            value function (0, inf)
        entropy_coef (float): Weight coefficient for entropy bonus [0, inf)
        update_interval (int): Model update interval in step
        minibatch_size (int): Minibatch size
        epochs (int): Training epochs in an update
        clip_eps (float): Epsilon for pessimistic clipping of likelihood ratio
            to update policy
        clip_eps_vf (float): Epsilon for pessimistic clipping of value
            to update value function. If it is ``None``, value function is not
            clipped on updates.
        standardize_advantages (bool): Use standardized advantages on updates
        average_v_decay (float): Decay rate of average V, only used for
            recording statistics
        average_loss_decay (float): Decay rate of average loss, only used for
            recording statistics
    """

    saved_attributes = ['model', 'optimizer']

    def __init__(self, model, optimizer,
                 gpu=None,
                 gamma=0.99,
                 lambd=0.95,
                 phi=lambda x: x,
                 value_func_coef=1.0,
                 entropy_coef=0.01,
                 update_interval=2048,
                 minibatch_size=64,
                 epochs=10,
                 clip_eps=0.2,
                 clip_eps_vf=None,
                 standardize_advantages=True,
                 average_v_decay=0.999, average_loss_decay=0.99,
                 batch_states=batch_states,
                 ):
        self.model = model

        if gpu is not None and gpu >= 0:
            cuda.get_device_from_id(gpu).use()
            self.model.to_gpu(device=gpu)

        self.num_envs = 1
        self.optimizer = optimizer
        self.gamma = gamma
        self.lambd = lambd
        self.phi = phi
        self.value_func_coef = value_func_coef
        self.entropy_coef = entropy_coef
        self.update_interval = update_interval
        self.minibatch_size = minibatch_size
        self.epochs = epochs
        self.clip_eps = clip_eps
        self.clip_eps_vf = clip_eps_vf
        self.standardize_advantages = standardize_advantages

        self.average_v = 0
        self._batch_init_loss_statistics(self.num_envs)
        self.average_v_decay = average_v_decay
        self.average_loss_decay = average_loss_decay

        self.batch_states = batch_states

        self.xp = self.model.xp
        self.last_state = None

        self.memory = []
        self.batch_memory = []
        self.last_episode = []
        self._accumulator = []
        self._accumulator_memory = []
        self._done_memory = None
        self._reset_memory = None

    def _act(self, state):
        xp = self.xp
        with chainer.using_config('train', False):
            b_state = self.batch_states([state], xp, self.phi)
            with chainer.no_backprop_mode():
                action_distrib, v = self.model(b_state)
                action = action_distrib.sample()
            return cuda.to_cpu(action.data)[0], cuda.to_cpu(v.data)[0]

    def _batch_act(self, states):
        """Runs a single-env self.model on a VectorEnv set of observations"""
        xp = self.xp
        states_ = [[state.astype('f')] for state in states]
        with chainer.using_config('train', False):
            b_state = self.batch_states(states_, xp, self.phi)
            with chainer.no_backprop_mode():
                action_distrib = [self.model(b)[0] for b in b_state]
                values = [self.model(b)[1] for b in b_state]
                actions_ = [a.sample() for a in action_distrib]

            batch_actions = xp.stack([action.data[0] for action in actions_])
            batch_v = xp.vstack([value.data[0] for value in values])
            return cuda.to_cpu(batch_actions), cuda.to_cpu(batch_v)

    def _train(self):

        interval = len(self.memory) + len(self.last_episode)

        if interval >= self.update_interval:
            self._flush_last_episode()
            self.update()
            self.memory = []

    def _batch_train(self, terminal):

        if self.batch_memory:

            mem_length = [len(mem) for mem in self.batch_memory]
            eps_length = [len(acc) for acc in self._accumulator_memory]
            interval = np.sum(mem_length) + np.sum(eps_length)

            if interval >= self.update_interval:
                self._batch_flush_last_episode(terminal, mem_length)
                self.batch_update()
                self.batch_memory = []

    def _flush_last_episode(self):
        if self.last_episode:
            self._compute_teacher(self.last_episode)
            self.memory.extend(self.last_episode)
            self.last_episode = []

    def _batch_flush_last_episode(self, terminal, mem_length=None):
        """Appends to accumulator and batch_memory

        The main idea is that the accumulator obtains the value
        of the previous episode, and appends it to batch_memory on the
        next timestep.
        """
        if mem_length is not None and np.any(np.array(mem_length) == 0):
            terminal = np.ones(self.num_envs, dtype=bool)
            if self._accumulator:
                self._batch_compute_teacher(self._accumulator_memory, terminal)
                self._batch_memory_append(self._accumulator_memory, terminal)
                self._clear_accum_memory(self._accumulator_memory, terminal)

        if self.last_episode:
            if self._accumulator:
                self._accumulator_memory_append(self._accumulator)
                self._batch_compute_teacher(self._accumulator_memory, terminal)
                self._batch_memory_append(self._accumulator_memory, terminal)
                self._clear_accum_memory(self._accumulator_memory, terminal)
                self._accumulator = []

            self._accumulator_append(self.last_episode)
            self.last_episode = []

    def _clear_accum_memory(self, accum_mem, terminal):
        for env, sig in enumerate(terminal):
            if sig:
                accum_mem[env] = []

    def _accumulator_memory_append(self, accum):
        """Appends to accumulator_memory before compute_teacher"""
        if not self._accumulator_memory:
            self._accumulator_memory.extend(accum)
        else:
            for env in range(self.num_envs):
                self._accumulator_memory[env].extend(accum[env])

    def _accumulator_append(self, last_episode):
        """Appends to self._accumulator for next_v_pred updates

        The type signature of batch_memory is:
            batch_memory :: [[dict, dict, ...], [dict, dict, ...]]
            where len(batch_memory) == self.num_envs

        Args:
            last_episode (list): a list of dictionaries with statistics
                from the last episode
        """
        if not self._accumulator:
            self._accumulator.extend(last_episode)
        else:
            assert len(self._accumulator) == self.num_envs
            for env in range(self.num_envs):
                self._accumulator[env].extend(last_episode[env])

    def _batch_memory_append(self, last_episode, terminal):
        """Appends to self.batch_memory

        The type signature of batch_memory is:
            batch_memory :: [[dict, dict, ...], [dict, dict, ...]]
            where len(batch_memory) == self.num_envs

        Args:
            last_episode (list): a list of dictionaries with statistics
                from the last episode
        """
        for env, sig in enumerate(terminal):
            if sig:
                self.batch_memory[env].extend(last_episode[env])

    def _compute_teacher(self, last_episode):
        """Estimate state values and advantages of self.last_episode

        TD(lambda) estimation
        """
        adv = 0.0
        for transition in reversed(last_episode):
            td_err = (
                transition['reward']
                + (self.gamma * transition['nonterminal']
                   * transition['next_v_pred'])
                - transition['v_pred']
            )
            adv = td_err + self.gamma * self.lambd * adv
            transition['adv'] = adv
            transition['v_teacher'] = adv + transition['v_pred']

    def _batch_compute_teacher(self, last_episode, terminal):
        """Estimate state values and advantages of self.last_episode

        TD(lambda) estimation
        """
        for env, sig in enumerate(terminal):
            if sig:
                self._compute_teacher(last_episode[env])

    def _batch_init_loss_statistics(self, num_envs):
        """Initialize loss statistics when batch"""
        self.average_loss_policy = np.zeros(shape=(num_envs,), dtype='f')
        self.average_loss_value_func = np.zeros(shape=(num_envs,), dtype='f')
        self.average_loss_entropy = np.zeros(shape=(num_envs,), dtype='f')

    def _lossfun(self,
                 distribs, vs_pred, log_probs,
                 vs_pred_old, target_log_probs,
                 advs, vs_teacher, idx=0):
        prob_ratio = F.exp(log_probs - target_log_probs)
        ent = distribs.entropy

        prob_ratio = F.expand_dims(prob_ratio, axis=-1)
        loss_policy = - F.mean(F.minimum(
            prob_ratio * advs,
            F.clip(prob_ratio, 1 - self.clip_eps, 1 + self.clip_eps) * advs))

        if self.clip_eps_vf is None:
            loss_value_func = F.mean_squared_error(vs_pred, vs_teacher)
        else:
            loss_value_func = F.mean(F.maximum(
                F.square(vs_pred - vs_teacher),
                F.square(_elementwise_clip(vs_pred,
                                           vs_pred_old - self.clip_eps_vf,
                                           vs_pred_old + self.clip_eps_vf)
                         - vs_teacher)
            ))
        loss_entropy = -F.mean(ent)

        self.average_loss_policy[idx] += (
            (1 - self.average_loss_decay) *
            (cuda.to_cpu(loss_policy.data) - self.average_loss_policy[idx]))
        self.average_loss_value_func[idx] += (
            (1 - self.average_loss_decay) *
            (cuda.to_cpu(loss_value_func.data) -
             self.average_loss_value_func[idx]))
        self.average_loss_entropy[idx] += (
            (1 - self.average_loss_decay) *
            (cuda.to_cpu(loss_entropy.data) - self.average_loss_entropy[idx]))

        return (
            loss_policy
            + self.value_func_coef * loss_value_func
            + self.entropy_coef * loss_entropy
        )

    def _batch_iter_reset(self, iter_list):
        """Resets a list of iterables"""
        for iterable in iter_list:
            iterable.reset()

    def _update(self, dataset_iter, target_model, mean_advs,
                std_advs, process_idx=None):
        """General update abstraction

        Args:
            dataset_iter (chainer.iterators.SerialIterator):
                the current memory for updating all parameters
            target_model (chainer.Model): model fed to the agent
            mean_advs (ndarray): list of computed mean advantages
            std_advs (ndarray): list of computed std advantages
            process_idx (int): process index for saving statistics
        """
        xp = self.xp

        while dataset_iter.epoch < self.epochs:
            batch = dataset_iter.__next__()
            states = self.batch_states(
                [b['state'] for b in batch], xp, self.phi)
            actions = xp.array([b['action'] for b in batch])
            distribs, vs_pred = self.model(states)
            with chainer.no_backprop_mode():
                target_distribs, _ = target_model(states)

            advs = xp.array([b['adv'] for b in batch], dtype=xp.float32)
            if self.standardize_advantages:
                advs = (advs - mean_advs[process_idx]) / std_advs[process_idx]

            vs_pred_old = xp.array([b['v_pred']
                                    for b in batch], dtype=xp.float32)
            vs_teacher = xp.array([b['v_teacher']
                                   for b in batch], dtype=xp.float32)

            vs_pred_old = xp.array([b['v_pred']
                                    for b in batch], dtype=xp.float32)
            vs_teacher = xp.array([b['v_teacher']
                                   for b in batch], dtype=xp.float32)

            self.optimizer.update(
                self._lossfun,
                distribs, vs_pred, distribs.log_prob(actions),
                vs_pred_old=vs_pred_old,
                target_log_probs=target_distribs.log_prob(actions),
                advs=advs,
                vs_teacher=vs_teacher,
                idx=process_idx
            )

    def update(self):
        """Performs a single environment update"""
        xp = self.xp

        # Set-up advantages
        if self.standardize_advantages:
            all_advs = xp.array([b['adv'] for b in self.memory])
            mean_advs = xp.mean(all_advs)
            std_advs = xp.std(all_advs)
        else:
            mean_advs, std_advs = (None,) * 2

        target_model = copy.deepcopy(self.model)

        # Make an iterator
        dataset_iter = chainer.iterators.SerialIterator(
            self.memory, self.minibatch_size)
        dataset_iter.reset()

        # Call _update() function
        self._update(dataset_iter, target_model, mean_advs, std_advs)

    def batch_update(self):
        """Performs a batch update"""
        xp = self.xp

        # Set-up advantages
        if self.standardize_advantages:
            all_advs = xp.array([e['adv']
                                 for env in self.batch_memory for e in env])
            mean_advs = xp.mean(all_advs)
            std_advs = xp.std(all_advs)

        else:
            mean_advs, std_advs = (None,) * 2

        target_model = copy.deepcopy(self.model)

        # Flatten self.batch_memory
        flat_batch_memory = [e for env in self.batch_memory for e in env]

        # Make an iterator
        dataset_iter = chainer.iterators.SerialIterator(
            flat_batch_memory, self.minibatch_size)
        dataset_iter.reset()

        # Call _update() function
        self._update(dataset_iter, target_model, mean_advs, std_advs)

    def act_and_train(self, obs, reward):
        if hasattr(self.model, 'obs_filter'):
            xp = self.xp
            b_state = self.batch_states([obs], xp, self.phi)
            self.model.obs_filter.experience(b_state)

        action, v = self._act(obs)

        # Update stats
        self.average_v += (
            (1 - self.average_v_decay) *
            (v[0] - self.average_v))

        if self.last_state is not None:
            self.last_episode.append({
                'state': self.last_state,
                'action': self.last_action,
                'reward': reward,
                'v_pred': self.last_v,
                'next_state': obs,
                'next_v_pred': v,
                'nonterminal': 1.0})
        self.last_state = obs
        self.last_action = action
        self.last_v = v

        self._train()
        return action

    def act(self, obs):
        action, v = self._act(obs)

        # Update stats
        self.average_v += (
            (1 - self.average_v_decay) *
            (v[0] - self.average_v))

        return action

    def batch_act(self, batch_obs):
        """Takes a batch of observations and peforms a batch of actions

        Args:
            batch_obs (ndarray): a list containing the observations

        Returns:
            batch_action (ndarray): set of actions for each environment
        """
        batch_action, batch_v = self._batch_act(batch_obs)

        return batch_action

    def batch_act_and_train(self, batch_obs):
        """Takes a batch of observations and performs a batch of actions

        Args:
            batch_obs (ndarray): a list containing the observations for each
                              environment.

        Returns:
            batch_action (ndarray): set of actions for each environment
        """
        # Infer number of envs
        self.num_envs = len(batch_obs)

        # Initialize loss stats
        if len(self.average_loss_policy) != self.num_envs:
            self._batch_init_loss_statistics(self.num_envs)
        # Initialize batch memory
        if not self.batch_memory:
            self.batch_memory = [[] for i in range(self.num_envs)]

        batch_action, batch_v = self._batch_act(batch_obs)

        # Update stats
        self.average_v += (
            (1 - self.average_v_decay) *
            (batch_v - self.average_v))

        self.last_state = batch_obs
        self.last_action = batch_action
        self.last_v = batch_v

        return batch_action

    def stop_episode_and_train(self, state, reward, done=False):
        _, v = self._act(state)

        assert self.last_state is not None
        self.last_episode.append({
            'state': self.last_state,
            'action': self.last_action,
            'reward': reward,
            'v_pred': self.last_v,
            'next_state': state,
            'next_v_pred': v,
            'nonterminal': 0.0 if done else 1.0})

        self.last_state = None
        del self.last_action
        del self.last_v

        self._flush_last_episode()
        self.stop_episode()

    def stop_episode(self):
        pass

    def _last_ep_append(self, i, done, batch_reward, batch_obs):
        """Helper function to append in last_episode"""
        return {
            'state': self.last_state[i],
            'action': self.last_action[i],
            'v_pred': self.last_v[i],
            'reward': batch_reward[i],
            'next_state': batch_obs[i],
            'nonterminal': 0.0 if done else 1.0
        }

    def batch_observe(self, batch_obs, batch_reward, batch_done, batch_info):
        """Proxy method during evaluation

        Args:
            batch_obs (ndarray): batch of observations
            batch_reward (ndarray): batch of rewards
            batch_done (ndarray): batch of done signals
            batch_info (ndarray): additional information
        """
        pass

    def batch_observe_and_train(self, batch_obs, batch_reward,
                                batch_done, batch_info):
        """Observe model interaction with env and updates it

        This method must be called after batch_act during training.

        Args:
            batch_obs (ndarray): batch of observations
            batch_reward (ndarray): batch of rewards
            batch_done (ndarray): batch of done signals
            batch_info (ndarray): additional information
        """
        batch_reset = np.array([info['reset'] for info in batch_info])

        # Initialize reset memory at first iteration
        if self._reset_memory is None:
            self._reset_memory = np.zeros((1, self.num_envs), dtype=bool)
        if self._done_memory is None:
            self._done_memory = np.zeros((1, self.num_envs), dtype=bool)

        if not self.last_episode:
            # Fill-in initial values first
            for i, done in enumerate(batch_done):
                self.last_episode.append([
                    self._last_ep_append(i, done,
                                         batch_reward,
                                         batch_obs)])
        else:
            # Then fill-in each list in envs
            for i, done in enumerate(batch_done):
                self.last_episode[i].append(
                    self._last_ep_append(i, done,
                                         batch_reward,
                                         batch_obs))

        if np.any(np.logical_or(batch_reset, batch_done)):
            # Call model whenever there is reset

            _, batch_v = self._batch_act(batch_obs)
            self.average_v += (
                (1 - self.average_v_decay) *
                (batch_v - self.average_v))

        for i, (reset, prev_reset) in enumerate(zip(batch_reset, self._reset_memory[-1])):  # NOQA
            # This episode's next_v_pred is new batch_v whenever reset
            self.last_episode[i][-1]['next_v_pred'] = batch_v[i] if reset else None  # NOQA
            try:
                # Previous episode's next_v_pred is this episode's
                # batch_v from batch_act() (lookback step)
                if not prev_reset:
                    # If not reset previously, use last_v
                    self._accumulator[i][-1]['next_v_pred'] = self.last_v[i]
            except IndexError:
                pass

        self._reset_memory = np.append(self._reset_memory,
                                       batch_reset[np.newaxis],
                                       axis=0)
        terminal = np.logical_or(self._done_memory[-1], self._reset_memory[-1])
        self._done_memory = np.append(self._done_memory,
                                      batch_done[np.newaxis],
                                      axis=0)

        self._batch_flush_last_episode(terminal)
        self._batch_train(terminal)

    def get_statistics(self):
        return [
            ('average_v', np.mean(self.average_v)),
            ('average_loss_policy', np.mean(self.average_loss_policy)),
            ('average_loss_value_func', np.mean(self.average_loss_value_func)),
            ('average_loss_entropy', np.mean(self.average_loss_entropy)),
        ]
