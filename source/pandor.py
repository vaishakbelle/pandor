"""
AND-OR search algorithm for a finite-state controller in a probabilistic environment

Based on ./dandor.py.
(Originally based on Hu and De Giacomo: A Generic Technique for Synthesizing Bounded Finite-State Controllers (2013).)

"""

import environments

import logging
from collections import OrderedDict

import time
from itertools import dropwhile, product


AND_FAILURE = -1
AND_UNKNOWN = 0
S_WIN = "win"
S_FAIL = "fail"
A_STOP = "stop"

# verbose flag
v = False
# v = True


class PandorControllerFound(Exception):
    pass


class PandorControllerNotFound(Exception):
    pass


class MealyController:
    """ An N-bounded Mealy machine
    States: 0, 1, 2, ... k-1  where k <= N
    Observations
    """
    def __init__(self, bound):
        self.bound = bound
        self.transitions = OrderedDict()

    @property
    def num_states(self):
        """ Counts the number of states already defined
        (returns 1 for the empty controller)
        """

        if len(self.transitions) == 0:
            return 1
        else:
            return max(q for q, _ in self.transitions.values()) + 1

    @property
    def init_state(self):
        return 0

    def __getitem__(self, item):
        """
        :type item: tuple of (contr_state, observation)
        """
        return self.transitions[item]

    def __setitem__(self, key, value):
        q, obs = key
        q_next, act = value

        assert q < self.num_states and q_next <= self.num_states, \
            "Invalid controller state transition: {} → {}".format(key, value)

        if not q_next < self.bound:
            assert q_next < self.bound

        self.transitions[key] = value

    def __str__(self):
        n = self.num_states
        s = f"States: {n}\n"
        for i in sorted(self.transitions.items(), key=lambda x: x[0][0] * n + x[1][0]):
            s += f"{i}\n"
        return s


class HistoryItem:
    def __init__(self, q, s, l):
        self.q = q
        self.s = s
        self.l = l

    def __str__(self):
        return f"(q: {self.q}, s: {self.s}, l: {self.l:0.3f})"

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        """ self.l == 99 is a wildcard"""
        return self.q == other.q and \
               self.s == other.s and \
               (self.l == 99 or self.l == other.l)


class StackItem:
    def __init__(self, history, alpha):
        self.history = history
        self.alpha = alpha

    def __eq__(self, other):
        return self.history == other.history and \
               all(self.alpha[key] == other.alpha[key] for key in self.alpha)


def set_test_controller(cont):
    env = environments.BridgeWalk()
    cont[0, False] = [1, env.A_LEFT]
    cont[1, False] = [1, env.A_FWD]
    cont[1, True] = [1, env.A_RIGHT]


class PAndOrPlanner:
    def __init__(self, env):
        self.env = env
        # attributes used by synth_plan() - def'd here only to suppress warnings
        self.contr, self.backtracking, self.backtrack_stack = None, None, None
        # Lower/upper bound for the LPC of the current controller
        self.lpc_desired = None
        self.alpha = None

    def synth_plan(self, states_bound, lpc_desired):
        self.backtracking = False
        self.backtrack_stack = []
        self.contr = MealyController(states_bound)
        self.lpc_desired = lpc_desired
        # counters for stats
        self.num_backtracking = 0
        self.num_steps = 0
        MAX_HISTORY_LENGTH = 1000
        self.alpha = {'win': [0.] * MAX_HISTORY_LENGTH,
                      'fail': [0.] * MAX_HISTORY_LENGTH,
                      'loop': [0.] * MAX_HISTORY_LENGTH,
                      'noter': [0.] * MAX_HISTORY_LENGTH}

        # set_test_controller(self.contr)

        # Note: for numerical stability, lpc_desired must be lower than 1.
        assert lpc_desired < 1.0

        try:
            self.and_step(self.contr.init_state, self.env.init_states_p, [], first_and_step=True)
        except PandorControllerFound:
            print("Controller found with max ", states_bound, "states.")
            return True
        except PandorControllerNotFound:
            print("No controller found with max ", states_bound, "states.")
            return False

        assert False, "This should not happen."  # len(self.backtrack_stack) > 0 ?

    def and_step(self, q, action, history, first_and_step=False):
        """
        :return:
            - FAILURE if self.lpc_max < self.lpc_desired
            - AND_UNKNOWN otherwise
        :raises:
            - PandorControllerFound if a sufficiently correct controller is found
        """

        if first_and_step:
            sl_next = self.env.init_states_p
        else:
            s = history[-1].s
            if action is A_STOP and s in self.env.goal_states:
                sl_next = [(S_WIN, 1.0)]
            elif action is A_STOP and s not in self.env.goal_states:
                sl_next = [(S_FAIL, 1.0)]
            else:
                sl_next = self.env.next_states_p(s, action)

        sl_next.sort(key=lambda sp: sp[1], reverse=True)

        def get_backtracked_iterator():
            # don't care about or_steps that succeeded and to which we don't want to backtrack
            # history == self.backtrack_stack[-1].history[0:len(history)]
            # so the relevant element of sl_next is
            #  self.backtrack_stack[-1].history[len(history)]
            return dropwhile(lambda x: x[0] != self.backtrack_stack[-1].history[len(history)].s,
                             iter(sl_next))

        for x in 'win', 'fail', 'noter', 'loop':
            self.alpha[x][len(history)] = 0.

        if self.backtracking:
            it = get_backtracked_iterator()
        else:
            it = iter(sl_next)

        list_it = list(it) # for debugging
        it = iter(list_it)

        while True:
            try:
                # Note: p = transition probability
                s_k, p_k = next(it)
            except StopIteration:
                # this cannot be the topmost AND step, because the early termination returns sooner
                assert len(history) > 0

                logging.info("AND: not fail at history %s", history) if v else 0

                # cumulate alpha values from last layer
                n = len(history)

                if len(history) == 1:
                    p_this = history[0].l
                else:
                    p_this = history[-1].l / history[-2].l

                for x in 'win', 'fail', 'noter':
                    self.alpha[x][n-1] += p_this * self.alpha[x][n] / (1 - self.alpha['loop'][n-1])

                # for x in 'win', 'fail', 'noter', 'loop':
                #     self.alpha[x][n] = 0.
                self.alpha['loop'][n-1] = 0.

                return AND_UNKNOWN

            if self.backtracking:
                logging.info("AND: Redoing s: %s", self.env.str_state(s_k)) if v else 0
            else:
                logging.info("AND: Simulating s: %s, q: %s", self.env.str_state(s_k), q) if v else 0

            self.or_step(q, s_k, p_k, history[:])

            likelihoods = self.calc_lambda(history)
            lpc_lower_bound = likelihoods['win']
            lpc_upper_bound = 1 - likelihoods['fail'] - likelihoods['noter']

            if lpc_lower_bound >= self.lpc_desired:
                logging.info("AND: succeed at history %s", history) if v else 0
                raise PandorControllerFound
            elif lpc_upper_bound < self.lpc_desired:
                logging.info("AND: fail at history %s", history) if v else 0
                self.backtracking = True
                self.num_backtracking += 1

                if len(self.backtrack_stack) == 0:
                    logging.info("AND: Trying to backtrack but empty stack; fail.") if v else 0
                    raise PandorControllerNotFound

                # decide if we should backtrack left or up
                # (ignore the last element of self.backtrack_stack[-1].history)
                # Note: could make it iterative `is` instead of equality
                if history == self.backtrack_stack[-1].history[:min(len(history),
                                                                    len(self.backtrack_stack[-1].history)-1)]:
                    it = get_backtracked_iterator()

                    # list_it = list(it)  # for debugging
                    # it = iter(list_it)
                    logging.info("AND: Backtracking left") if v else 0
                else:
                    logging.info("AND: Backtracking up") if v else 0
                    return AND_FAILURE
                # We set self.backtracking = False in or_step
                #   when we start doing business as usual:
                #   i.e. either when we arrive in an OR node from up
                #     or when we arrive in it from the left

    def or_step(self, q, s, p, history):
        """
        :param p: probability of next state transition
        :return: None
        """
        l = (history[-1].l * p) if len(history) > 0 else p
        # for debugging/stats only
        q_next_last, action_last = None, None
        self.num_steps += 1

        if s is S_WIN:
            # len(history) is good because hist does not yet contain this step.
            self.alpha['win'][len(history)] += p
            logging.info("OR: terminated in goal state") if v else 0
            return

        elif s is S_FAIL:
            self.alpha['fail'][len(history)] += p
            logging.info("OR: terminated in NOT goal state") if v else 0
            return

        elif HistoryItem(q, s, l) in history:
            self.alpha['noter'][len(history)] += p
            logging.info("OR: repeated state") if v else 0
            return

        elif HistoryItem(q, s, 99) in history:
            looping_timestep = history.index(HistoryItem(q,s,99))
            self.alpha['loop'][looping_timestep] += l / history[looping_timestep].l
            logging.info("OR: loop to level %d", looping_timestep) if v else 0
            return

        history.append(HistoryItem(q, s, l))
        obs = self.env.get_obs(s)

        # if not backtracking or we did not make a nondet choice last time here
        if  ((not self.backtracking) and ((q, obs) in self.contr.transitions)) or \
            (self.backtracking and (not any(history == bt_item.history
                                            for bt_item in self.backtrack_stack))):
            q_next, action = self.contr[q, obs]
            if (action not in self.env.legal_actions(s)) and not (action is A_STOP):
                self.alpha['fail'][len(history) - 1] += p
                logging.info("OR: illegal action {} in state {}".format(action, s))
                return

            res = self.and_step(q_next, action, history)
            logging.info("OR: AND returned {} ".format(res) +
                         ("and now backtracking" if self.backtracking else "")) if v else 0
            return

        # otherwise we make a nondet choice
        if not self.backtracking:
            # no (q_next,act) defined for (q,obs) ⇒ define new one with this iterator
            it = self.get_mealy_qa_iterator(s)

            # store a new checkpoint iff we're not backtracking currently
            alpha_copy = {}
            for key in self.alpha:
                alpha_copy[key] = self.alpha[key][:]
            self.backtrack_stack.append(StackItem(history[:], alpha_copy))
            logging.info("OR: checkpoint at q: %s, s: %s\n    with history %s",
                         q, self.env.str_state(s), history) if v else 0

        else:  # backtracking
            # this is the node of the last checkpoint
            # (enough to check the length because we're in the right branch now)
            if len(history) == len(self.backtrack_stack[-1].history):
                self.backtracking = False
                self.revert_variables()

                t = self.contr.transitions.popitem()
                assert (q, obs) == t[0]

                q_next_last, action_last = t[1]

                logging.info("OR: (redoing) Deleted: (%s,%s) -> (%s,%s)",
                             q, self.env.str_obs(obs),
                             q_next_last, self.env.str_action(action_last)) if v else 0

                it = self.get_mealy_qa_iterator(s,
                                                q_next_last,
                                                lambda x: x[1] != action_last)

                # burn the controller extension that caused the trouble earlier
                _ = next(it)
            else:
                # the controller transition and action should already be defined
                assert (q, obs) in self.contr.transitions
                q_next_last, action_last = self.contr[q, obs]
                assert action_last in self.env.legal_actions(s) or action_last is A_STOP

                logging.info("OR: redoing: (%s,%s) -> (%s,%s)",
                             q, self.env.str_obs(obs),
                             q_next_last, self.env.str_action(action_last)) if v else 0

                it = self.get_mealy_qa_iterator(s,
                                                q_next_last,
                                                lambda x: x[1] != action_last)

        # non-det branching of q',a
        # save function arguments for bracktracking (history is already in backtrack_stack)
        s_saved = s
        q_saved = q

        # list_it = list(it)  # for debugging
        # it = iter(list_it)

        # important: q_next first, so we only add new states when necessary
        for q_next, action in it:
            # extend controller if not backtracking
            if not self.backtracking:
                self.contr[q, obs] = q_next, action
                logging.info("OR: Added:   (%s,%s) -> (%s,%s)",
                             q, self.env.str_obs(obs),
                             q_next, self.env.str_action(action)) if v else 0

            if self.and_step(q_next, action, history) == AND_UNKNOWN:
                # If we're here then self.backtracking is already False
                #   (if we came from up, then we cleared it;
                #    if we came from left, then the above call just succeeded
                assert not self.backtracking
                logging.info("OR: AND step didn't fail") if v else 0
                return
            else:
                # set backtracking to False: either there are more AND branches to try,
                #   or we'll set it to true in the and_step above.
                self.backtracking = False
                self.revert_variables()

                # restore saved values
                q, s = q_saved, s_saved
                len_history = len(self.backtrack_stack[-1].history)

                logging.info("OR: Backstep: %s at (q {}, s {}) with len {}".format(q,s, len_history),
                             [ (x.q, self.env.str_state(x.s), x.l) for x in history[len_history:] ]) if v else 0

                # # it's enough to clip the history if we're backtracking in the or_step
                # del history[len_history:]
                # but it's not needed here, is it. see lines below the for loop

                t = self.contr.transitions.popitem()

                logging.info("OR: Deleted: (%s,%s) -> (%s,%s)",
                             t[0][0], self.env.str_obs(t[0][1]),
                             t[1][0], self.env.str_action(t[1][1])) if v else 0

        # self.backtracking = True
        self.revert_variables()
        self.backtrack_stack.pop()
        self.alpha['fail'][len(history) - 1] += p
        logging.info("OR: all extensions failed, new upper bound: %0.3f", self.lpc_upper_bound) if v else 0
        return

    def calc_lambda(self, history):
        assert len(history) >= 1

        n = len(history)
        likelihoods = {}
        for x in 'win', 'fail', 'noter':
            likelihoods[x] = self.alpha[x][n]
        for k in range(n-1, -1, -1):
            if k == 0:
                p_k = history[k].l
            else:
                p_k = history[k].l / history[k-1].l

            for key in likelihoods:
                likelihoods[key] = self.alpha[key][k] + \
                                    p_k * likelihoods[key] / (1 - self.alpha['loop'][k])
        return likelihoods

    def revert_variables(self):
        for key in self.alpha:
            self.alpha[key] = self.backtrack_stack[-1].alpha[key]

    def get_mealy_qa_iterator(self, s, q_next_last=0, drop_func=lambda x: False):
        if s in self.env.goal_states:
            legal_acts = [A_STOP] + self.env.legal_actions(s)
        else:
            legal_acts = self.env.legal_actions(s) + [A_STOP]
        it = dropwhile(drop_func,
                       product(range(q_next_last, min(self.contr.bound, self.contr.num_states + 1)),
                               legal_acts))

        return it


if __name__ == '__main__':
    if v:
        logging.basicConfig(level=logging.INFO)

    env = environments.BridgeWalk(4)

    planner = PAndOrPlanner(env)
    success = planner.synth_plan(states_bound=2, lpc_desired=0.7)

    time.sleep(1)  # Wait for mesages of logging module
    if success:
        print(planner.contr)

    print("Num. of backtracks: {}".format(planner.num_backtracking))
    print("Num. of steps taken: {}".format(planner.num_steps))
    # print("LPC upper: {:.3f}".format(planner.lpc_upper_bound))
    # print("LPC lower: {:.3f}".format(planner.lpc_lower_bound))
