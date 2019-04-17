import collections
from typing import Iterable
import numpy as np

from ..errors import LimitExceeded
from ..game import Game
from ..situation import Situation, StateInfo
from ..strategy import Strategy
from ..utils import get_rng, Distribution
from .stats import sample_payoff
from .mccfr import OutcomeMCCFR, RegretStrategy
from ..adapter import Adapter
from ..observation import Observation

SupportItem = collections.namedtuple("SupportItem", ["situation", "probability"])


class BestResponse(Strategy):
    """
    Compute a best-response strategy by game tree traversal.

    May be very computationaly demanding as it traverses the whole tree on creation.
    `strategies[player]` is ignored and may be e.g. `None`.
    """

    DEFAULT_ADAPTER = "HashableAdapter"

    def __init__(self,
                 game: Game,
                 player: int,
                 strategies: Iterable[Strategy],
                 *,
                 adapter: Adapter = None,
                 max_nodes=1e6):
        super().__init__(game, adapter)
        assert player < self.game.players and player >= 0
        assert len(strategies) == game.players
        nodes = 0

        # DFS for from situation to terminal or `player`'s situation
        def trace(situation, probability, supports):
            nonlocal nodes
            nodes += 1
            if nodes > max_nodes:
                raise LimitExceeded(
                    "BestResponse traversed more than allowed {} nodes.".format(max_nodes) +
                    "Either increase the limit or consider using approximate best response.")
            # Just to get rid of nodes where distrbution returned pure zero
            if probability == 0.0:
                return 0.0
            p = situation.player
            if p == player:
                pi = self.adapter.get_observation(situation, player).data
                s = supports.setdefault(pi, list())
                s.append(SupportItem(situation, probability))
                return 0
            if p == StateInfo.TERMINAL:
                return situation.payoff[player] * probability
            if p == StateInfo.CHANCE:
                dist = Distribution(situation.actions, situation.chance)
            else:
                dist = strategies[p].get_policy(situation)
            return sum(
                trace(self.game.play(situation, action), pr * probability, supports)
                for action, pr in dist.items())

        # DFS from isets to other isets of "player"
        def traverse(iset, support):
            actions = support[0].situation.actions
            values = []
            br_list = []
            for action in actions:
                new_supports = {}
                value = 0
                best_responses = {}
                br_list.append(best_responses)

                for s in support:
                    value += trace(self.game.play(s.situation, action), s.probability,
                                   new_supports)
                for iset2, s in new_supports.items():
                    v, br = traverse(iset2, s)
                    value += v
                    best_responses.update(br)

                values.append(value)

            values = np.array(values)
            mx = values.max()
            is_best = values >= (mx - mx * 0e-6)
            br_result = {}
            bdist = is_best.astype(np.float)
            br_result[iset] = bdist / sum(bdist)
            for br, is_b in zip(br_list, is_best):
                if is_b:
                    br_result.update(br)
            return mx, br_result

        supports = {}
        self.best_responses = {}
        value = trace(self.game.start(), 1.0, supports)
        for iset2, s in supports.items():
            v, br = traverse(iset2, s)
            value += v
            self.best_responses.update(br)
        self.value = value

    def _strategy(self, observation, n_active, situation=None):
        return self.best_responses[observation]


class ApproxBestResponse(Strategy):
    """
    Compute an approximate best-response strategy using MCCFR.

    Uses given number of iterations of OutcomeMCCFR.
    `strategies[player]` is ignored and may be e.g. `None`.
    """

    DEFAULT_ADAPTER = "HashableAdapter"

    def __init__(self,
                 game: Game,
                 player: int,
                 strategies: Iterable[Strategy],
                 iterations: int,
                 *,
                 adapter: Adapter = None,
                 seed=None,
                 rng=None):
        super().__init__(game, adapter)
        self.rng = get_rng(seed=seed, rng=rng)
        self.player = player
        self.strategies = list(strategies)
        self.strategies[self.player] = RegretStrategy(self.game, self.adapter)
        self.mccfr = OutcomeMCCFR(self.game, self.strategies, [self.player], rng=self.rng)
        self.mccfr.compute(iterations, burn=0.5)

    def make_policy(self, observation: Observation) -> Distribution:
        return self.strategies[self.player].make_policy(observation)

    def sample_value(self, iterations):
        val = sample_payoff(self.game, self.strategies, iterations, rng=self.rng)[0][self.player]
        return val


def exploitability(game: Game,
                   measured_player: int,
                   strategy: Strategy,
                   *,
                   adapter: Adapter = None,
                   max_nodes: float = 1e6):
    """
    Exact exploitability of a player strategy in a two player ZERO-SUM game.
    """
    assert measured_player in (0, 1)
    assert game.players == 2
    assert isinstance(strategy, Strategy)
    br = BestResponse(game,
                      1 - measured_player, [strategy, strategy],
                      adapter=adapter,
                      max_nodes=max_nodes)
    return br.value


def approx_exploitability(game: Game,
                          measured_player: int,
                          strategy: Strategy,
                          iterations: float,
                          *,
                          adapter: Adapter = None,
                          seed=None,
                          rng=None):
    """
    Approximate exploitability of a player strategy in a two player ZERO-SUM game.

    Uses given number of iterations of OutcomeMCCFR.
    The value is then taken from a mean of `iterations / 4` plays.
    Note that the "best-response" strategy may be worse than the original if the
    iteration number is too small.
    """
    assert game.players == 2
    assert measured_player in (0, 1)
    br = ApproxBestResponse(game,
                            1 - measured_player, [strategy, strategy],
                            iterations,
                            rng=rng, seed=seed,
                            adapter=adapter)
    return br.sample_value(iterations // 2)
