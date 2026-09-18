"""
Optimal starting lineup selection.

Picking a lineup is an assignment problem: every starting slot must be
filled by exactly one eligible player, each player fills at most one slot,
and we want the highest total projection. Greedy filling gets this wrong
whenever a FLEX is involved, so this solves it exactly with a min-cost
max-flow, which is instant at fantasy roster sizes.
"""

from collections import deque

from . import constants as C
from .winprob import lineup_distribution, win_probability


# Projections are floats; the flow solver works in integers to stay exact.
POINT_SCALE = 100


class MinCostFlow:
    """Successive shortest paths with SPFA, which tolerates negative costs."""

    def __init__(self, node_count):
        self.node_count = node_count
        self.graph = [[] for _ in range(node_count)]

    # Each edge records the index of its reverse edge so residual capacity
    # can be updated in place during augmentation.
    def add_edge(self, source, target, capacity, cost):
        self.graph[source].append([target, capacity, cost, len(self.graph[target])])
        self.graph[target].append([source, 0, -cost, len(self.graph[source]) - 1])

    # Cheapest path from source to sink across edges that still have capacity.
    def _find_path(self, source, sink):
        inf = float("inf")
        dist = [inf] * self.node_count
        in_queue = [False] * self.node_count
        prev_node = [-1] * self.node_count
        prev_edge = [-1] * self.node_count

        dist[source] = 0
        queue = deque([source])
        in_queue[source] = True

        while queue:
            node = queue.popleft()
            in_queue[node] = False

            for edge_index, edge in enumerate(self.graph[node]):
                target, capacity, cost, _ = edge
                if capacity <= 0:
                    continue
                if dist[node] + cost >= dist[target]:
                    continue

                dist[target] = dist[node] + cost
                prev_node[target] = node
                prev_edge[target] = edge_index
                if not in_queue[target]:
                    queue.append(target)
                    in_queue[target] = True

        if dist[sink] == inf:
            return None
        return prev_node, prev_edge

    # Push flow along cheapest paths until the sink is unreachable.
    def run(self, source, sink):
        while True:
            found = self._find_path(source, sink)
            if not found:
                break
            prev_node, prev_edge = found

            # Walk the path backwards to find its bottleneck capacity.
            bottleneck = float("inf")
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                bottleneck = min(bottleneck, edge[1])
                node = prev_node[node]

            # Apply that much flow along the same path.
            node = sink
            while node != source:
                edge = self.graph[prev_node[node]][prev_edge[node]]
                edge[1] -= bottleneck
                self.graph[node][edge[3]][1] += bottleneck
                node = prev_node[node]

    # Flow carried on the forward edge from one node to another, if any.
    def flow_between(self, source, target):
        for edge_target, capacity, cost, rev_index in self.graph[source]:
            if edge_target != target:
                continue
            reverse_capacity = self.graph[target][rev_index][1]
            if reverse_capacity > 0:
                return reverse_capacity
        return 0


# The projection the optimizer scores a player by. The default is this
# week's projection, zeroed out for byes and players ruled out.
def week_projection(player):
    return player.effective_projection


# Rest-of-season projection, for waiver decisions that are not about one week.
def season_projection(player):
    if player.season_projected_points:
        return player.season_projected_points
    return player.projected_points


class LineupResult:
    """The best lineup found, plus what it would take to get there."""

    def __init__(
        self,
        assignments,
        bench,
        total,
        current_total,
        moves,
        objective="points",
        win_probability=None,
        current_win_probability=None,
    ):
        self.assignments = assignments
        self.bench = bench
        self.total = total
        self.current_total = current_total
        self.moves = moves
        self.objective = objective
        self.win_probability = win_probability
        self.current_win_probability = current_win_probability

    @property
    def points_gained(self):
        return self.total - self.current_total

    def to_dict(self):
        result = {
            "objective": self.objective,
            "projected_total": round(self.total, 2),
            "current_projected_total": round(self.current_total, 2),
            "points_gained": round(self.points_gained, 2),
            "is_already_optimal": not self.moves,
            "lineup": [
                {
                    "slot_id": slot_id,
                    "slot_name": C.slot_name(slot_id),
                    "player": player.to_dict() if player else None,
                }
                for slot_id, player in self.assignments
            ],
            "bench": [p.to_dict() for p in self.bench],
            "moves": self.moves,
        }
        if self.win_probability is not None:
            result["win_probability"] = round(self.win_probability, 3)
            result["current_win_probability"] = round(self.current_win_probability, 3)
        return result


# Assign players to slot groups, minimizing the total of cost_fn. Returns a
# map from slot id to the players placed in it, plus the indexes used.
def _assign(players, slot_ids, slot_capacity, cost_fn):
    player_count = len(players)
    source = 0
    sink = player_count + len(slot_ids) + 1

    def player_node(index):
        return 1 + index

    def slot_node(index):
        return 1 + player_count + index

    flow = MinCostFlow(sink + 1)

    for index, player in enumerate(players):
        flow.add_edge(source, player_node(index), 1, 0)

        eligible = set(player.startable_slots())
        for slot_index, slot_id in enumerate(slot_ids):
            if slot_id not in eligible:
                continue
            flow.add_edge(
                player_node(index),
                slot_node(slot_index),
                1,
                cost_fn(player, slot_id),
            )

    for slot_index, slot_id in enumerate(slot_ids):
        flow.add_edge(slot_node(slot_index), sink, slot_capacity[slot_id], 0)

    flow.run(source, sink)

    filled = {slot_id: [] for slot_id in slot_ids}
    used_indexes = set()

    for index in range(player_count):
        for slot_index, slot_id in enumerate(slot_ids):
            if flow.flow_between(player_node(index), slot_node(slot_index)) > 0:
                filled[slot_id].append(players[index])
                used_indexes.add(index)
                break

    return filled, used_indexes


# Solve the assignment. Returns a list of (slot_id, player-or-None) pairs,
# one entry per starting slot, plus everyone left on the bench.
def solve_lineup(players, starting_slots, projection_fn=week_projection):
    players = list(players)
    if not starting_slots:
        return [], players

    # Collapse repeated slots into groups with a capacity, so two RB slots
    # are one node with capacity 2 rather than two interchangeable nodes.
    slot_ids = []
    slot_capacity = {}
    for slot_id in starting_slots:
        if slot_id not in slot_capacity:
            slot_capacity[slot_id] = 0
            slot_ids.append(slot_id)
        slot_capacity[slot_id] += 1

    # First pass: choose which players start, maximizing projected points.
    def points_cost(player, slot_id):
        return -int(round(projection_fn(player) * POINT_SCALE))

    _, assigned_indexes = _assign(players, slot_ids, slot_capacity, points_cost)

    # Once the set of starters is settled the total is fixed, because a
    # player scores the same wherever they are slotted. So the second pass
    # re-seats exactly those players, preferring the slots they already hold.
    # That keeps the recommendation free of swaps that change nothing.
    chosen = [players[i] for i in sorted(assigned_indexes)]

    def retention_cost(player, slot_id):
        return -1 if player.lineup_slot == slot_id else 0

    filled, _ = _assign(chosen, slot_ids, slot_capacity, retention_cost)

    # Emit one row per starting slot, in the league's slot order.
    assignments = []
    queues = {slot_id: list(group) for slot_id, group in filled.items()}
    for slot_id in starting_slots:
        queue = queues.get(slot_id) or []
        assignments.append((slot_id, queue.pop(0) if queue else None))

    bench = [p for i, p in enumerate(players) if i not in assigned_indexes]
    return assignments, bench


# Work out the slot changes that turn the current lineup into the target one.
# ESPN applies these as one transaction, so a straight swap is two items.
def build_moves(assignments, bench, locked_player_ids=None):
    locked = set(locked_player_ids or [])

    target_slot = {}
    for slot_id, player in assignments:
        if player:
            target_slot[player.player_id] = slot_id
    for player in bench:
        target_slot[player.player_id] = C.BENCH_SLOT

    moves = []
    for slot_id, player in assignments:
        if not player:
            continue
        if player.lineup_slot != slot_id and player.player_id not in locked:
            moves.append(
                {
                    "player_id": player.player_id,
                    "player_name": player.name,
                    "from_slot": player.lineup_slot,
                    "from_slot_name": C.slot_name(player.lineup_slot),
                    "to_slot": slot_id,
                    "to_slot_name": C.slot_name(slot_id),
                }
            )

    for player in bench:
        # Leave injured reserve alone; ESPN manages that slot separately.
        if player.lineup_slot == C.IR_SLOT:
            continue
        if player.lineup_slot != C.BENCH_SLOT and player.player_id not in locked:
            moves.append(
                {
                    "player_id": player.player_id,
                    "player_name": player.name,
                    "from_slot": player.lineup_slot,
                    "from_slot_name": C.slot_name(player.lineup_slot),
                    "to_slot": C.BENCH_SLOT,
                    "to_slot_name": C.slot_name(C.BENCH_SLOT),
                }
            )

    return moves


OBJECTIVE_POINTS = "points"
OBJECTIVE_WIN = "win"


# Chance a set of starters beats an opponent, given as a (mean, stddev) pair.
def _win_chance(starters, opponent):
    mean, stddev = lineup_distribution(starters)
    return win_probability(mean, opponent[0], stddev, opponent[1])


# Start from the points-optimal lineup and keep making the single starter-for-
# bench swap that most raises the chance of winning, until none does. Each
# swap is checked with the same solver to make sure the new set of starters
# can still fill every slot. Underdogs drift toward boom-or-bust players and
# favorites toward steady ones, at whatever cost in projected points the
# win chance justifies.
# Locked starters cannot move but still score, so they count toward the
# distribution through `fixed`.
def improve_for_win(
    assignments, bench, slots, opponent, projection_fn=week_projection, fixed=()
):
    fixed = list(fixed)
    starters = [p for _, p in assignments if p]
    bench = [p for p in bench if projection_fn(p) > 0]
    current = _win_chance(starters + fixed, opponent)

    while True:
        best_chance = current + 1e-6
        best_swap = None
        for out_player in starters:
            for in_player in bench:
                trial = [p for p in starters if p is not out_player] + [in_player]
                trial_assignments, _ = solve_lineup(trial, slots, projection_fn)
                if any(player is None for _, player in trial_assignments):
                    continue
                chance = _win_chance(trial + fixed, opponent)
                if chance > best_chance:
                    best_chance = chance
                    best_swap = (out_player, in_player)
        if best_swap is None:
            break
        out_player, in_player = best_swap
        starters = [p for p in starters if p is not out_player] + [in_player]
        bench = [p for p in bench if p is not in_player] + [out_player]
        current = best_chance

    return solve_lineup(starters, slots, projection_fn)[0]


# Full optimization for one team: solve, diff against the current lineup,
# and report the point swing. With objective="win" and an opponent given as
# a (mean, stddev) pair, the lineup is tuned for the chance of winning that
# matchup instead of for raw projected points.
def optimize_team(
    team,
    starting_slots,
    projection_fn=week_projection,
    locked_player_ids=None,
    extra_players=None,
    exclude_player_ids=None,
    objective=OBJECTIVE_POINTS,
    opponent=None,
):
    exclude = set(exclude_player_ids or [])
    locked = set(locked_player_ids or [])
    use_win = objective == OBJECTIVE_WIN and opponent is not None

    # IR players cannot start, and locked starters cannot be moved out.
    candidates = []
    for player in team.roster:
        if player.player_id in exclude:
            continue
        if player.lineup_slot == C.IR_SLOT:
            continue
        candidates.append(player)

    for player in extra_players or []:
        if player.player_id not in exclude:
            candidates.append(player)

    # A locked starter keeps its slot, so remove both the player and the slot
    # from the problem before solving.
    remaining_slots = list(starting_slots)
    forced = []
    for player in list(candidates):
        if player.player_id in locked and player.is_starting:
            if player.lineup_slot in remaining_slots:
                remaining_slots.remove(player.lineup_slot)
                forced.append((player.lineup_slot, player))
                candidates.remove(player)

    assignments, bench = solve_lineup(candidates, remaining_slots, projection_fn)
    if use_win:
        assignments = improve_for_win(
            assignments,
            bench,
            remaining_slots,
            opponent,
            projection_fn,
            fixed=[p for _, p in forced],
        )
        seated = {id(p) for _, p in assignments if p}
        bench = [p for p in candidates if id(p) not in seated]
    assignments = forced + assignments

    total = sum(projection_fn(p) for _, p in assignments if p)
    current_total = sum(
        projection_fn(p) for p in team.roster if p.is_starting and p.player_id not in exclude
    )
    moves = build_moves(assignments, bench, locked_player_ids=locked)

    win_chance = current_win_chance = None
    if use_win:
        win_chance = _win_chance([p for _, p in assignments if p], opponent)
        current_win_chance = _win_chance(
            [p for p in team.roster if p.is_starting and p.player_id not in exclude],
            opponent,
        )

    return LineupResult(
        assignments,
        bench,
        total,
        current_total,
        moves,
        objective=OBJECTIVE_WIN if use_win else OBJECTIVE_POINTS,
        win_probability=win_chance,
        current_win_probability=current_win_chance,
    )


# The best total a roster could put up this week, ignoring what is currently
# set. Used to score other teams and to value waiver targets.
def best_possible_total(players, starting_slots, projection_fn=week_projection):
    startable = [p for p in players if p.lineup_slot != C.IR_SLOT]
    assignments, _ = solve_lineup(startable, starting_slots, projection_fn)
    return sum(projection_fn(p) for _, p in assignments if p)


# Chance a roster's points-optimal lineup beats an opponent given as a
# (mean, stddev) pair. Cheaper than the full win search, so it is what waiver
# scoring uses.
def best_lineup_win_probability(players, starting_slots, opponent, projection_fn=week_projection):
    startable = [p for p in players if p.lineup_slot != C.IR_SLOT]
    assignments, _ = solve_lineup(startable, starting_slots, projection_fn)
    return _win_chance([p for _, p in assignments if p], opponent)
