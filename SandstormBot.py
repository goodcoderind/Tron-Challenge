from collections import deque


class Bot:
    DIRS = {
        "N": (0, -1),
        "S": (0, 1),
        "W": (-1, 0),
        "E": (1, 0),
    }
    OPPOSITE = {"N": "S", "S": "N", "W": "E", "E": "W"}
    INF = 10 ** 9

    def __init__(self):
        self.direction = None
        self.last_pos = None
        self.turn_count = 0
        self.phase_uses = 3
        self.emp_uses = 0
        self.last_emp_turn = -1000
        self.last_map_name = None
        self.last_scores = None
        self.heads_are_walkable = False
        self.solo_target = None
        self.enemy_opening_moves = {}
        self.last_seen_enemy_positions = {}

    # -----------------------------
    # Public entrypoint
    # -----------------------------
    def get_move(self, board, player_id, pos, info) -> str:
        try:
            return self._get_move_impl(board, player_id, pos, info or {})
        except Exception:
            return self._emergency_fallback(board, pos)

    # -----------------------------
    # Core move selection
    # -----------------------------
    def _get_move_impl(self, board, player_id, pos, info):
        rows = len(board) if board else 0
        cols = len(board[0]) if rows and board[0] else 0
        if rows == 0 or cols == 0:
            return self._finalize_move("N", pos, str(info.get("map_name", "")), info)

        if not self._in_bounds(pos[0], pos[1], rows, cols):
            return self._finalize_move("N", pos, str(info.get("map_name", "")), info)

        map_name = str(info.get("map_name", ""))
        map_key = self._normalize_map_name(map_name)
        if self._should_reset(map_name, pos, info):
            self._reset_state()

        self.turn_count += 1
        self._refresh_direction_from_position(pos)
        if self.direction is None:
            self.direction = self._initial_direction(player_id)

        player_positions = self._extract_player_positions(info, board, rows, cols)
        player_positions[player_id] = pos
        enemy_positions = {}
        for pid, head in player_positions.items():
            if pid == player_id:
                continue
            if head is not None and self._in_bounds(head[0], head[1], rows, cols):
                enemy_positions[pid] = head

        solo_mode = map_key.startswith("s_")
        self.heads_are_walkable = solo_mode
        if solo_mode:
            enemy_positions = {}
        else:
            self.solo_target = None
        duel_mode = len(enemy_positions) <= 1
        battle_survival_mode = (not solo_mode) and len(enemy_positions) >= 3

        occupied_heads = set(enemy_positions.values())
        occupied_now = set(occupied_heads)
        occupied_now.add(pos)

        self._remember_enemy_directions(info)

        opening_move = self._opening_book_move(
            board,
            info,
            player_id,
            pos,
            map_key,
            occupied_heads,
            rows,
            cols,
        )
        if opening_move is not None:
            return self._finalize_move(opening_move, pos, map_name, info)

        duel_enemy_pos = None
        baseline_enemy_area = 0
        if duel_mode and len(enemy_positions) == 1:
            duel_enemy_pos = next(iter(enemy_positions.values()))
            baseline_enemy_area = self._reachable_area_from_source(
                board, duel_enemy_pos, occupied_now, rows, cols
            )

        enemy_dist, enemy_owner, enemy_next_owner = self._build_enemy_maps(
            board, pos, enemy_positions, occupied_heads
        )
        collectible_bias = self._build_collectible_bias(
            board,
            pos,
            occupied_now,
            enemy_dist,
            enemy_owner,
            player_id,
            map_name,
            enemy_positions,
            battle_survival_mode,
            rows,
            cols,
        )
        weights = self._mode_weights(solo_mode, duel_mode, battle_survival_mode)

        normal_moves = {}
        best_normal = None
        for move, (dx, dy) in self.DIRS.items():
            nx = pos[0] + dx
            ny = pos[1] + dy
            if not self._is_safe_normal_step(
                board, nx, ny, move, occupied_heads, rows, cols
            ):
                continue

            metrics = self._analyze_candidate(
                board,
                (nx, ny),
                occupied_now,
                enemy_dist,
                enemy_owner,
                player_id,
                rows,
                cols,
            )
            if metrics["area"] <= 0:
                continue

            lookahead = self._count_safe_neighbors(
                board, (nx, ny), occupied_now, rows, cols
            )
            immediate_reward = self._cell_reward(board[ny][nx])
            enemy_d = enemy_dist[ny][nx]
            contested_by = enemy_next_owner.get((nx, ny))
            nearest_enemy = self._nearest_enemy_distance((nx, ny), enemy_positions)
            enemy_space_reduction = 0.0
            if duel_enemy_pos is not None:
                blocked_after = set(occupied_now)
                blocked_after.add((nx, ny))
                enemy_area_after = self._reachable_area_from_source(
                    board, duel_enemy_pos, blocked_after, rows, cols
                )
                enemy_space_reduction = max(0.0, baseline_enemy_area - enemy_area_after)

            score = self._score_position(
                metrics,
                lookahead,
                immediate_reward,
                collectible_bias.get(move, 0.0),
                enemy_d,
                contested_by,
                nearest_enemy,
                player_id,
                weights,
                map_name,
                move,
                enemy_space_reduction,
            )

            entry = {
                "move": move,
                "target": (nx, ny),
                "score": score,
                "lookahead": lookahead,
                "immediate_reward": immediate_reward,
                "enemy_d": enemy_d,
                "contested_by": contested_by,
                "nearest_enemy": nearest_enemy,
                "enemy_space_reduction": enemy_space_reduction,
            }
            entry.update(metrics)
            normal_moves[move] = entry

            if best_normal is None or entry["score"] > best_normal["score"]:
                best_normal = entry

        if solo_mode and normal_moves:
            self._apply_solo_greedy_scoring(
                board,
                pos,
                occupied_now,
                normal_moves,
                map_name,
                rows,
                cols,
            )
            best_normal = None
            for entry in normal_moves.values():
                if best_normal is None or entry["score"] > best_normal["score"]:
                    best_normal = entry
        elif duel_mode and len(enemy_positions) == 1 and normal_moves:
            if map_key == "treasure":
                center = (cols // 2, rows // 2)
                duel_coin_bias = self._build_duel_coin_bias(
                    board,
                    pos,
                    duel_enemy_pos,
                    normal_moves,
                    rows,
                    cols,
                )
                ranked_moves = []
                for entry in normal_moves.values():
                    enemy_area_after = baseline_enemy_area - entry["enemy_space_reduction"]
                    entry["duel_enemy_area"] = enemy_area_after
                    center_distance = abs(entry["target"][0] - center[0]) + abs(entry["target"][1] - center[1])
                    blocked_after = set(occupied_now)
                    blocked_after.add(entry["target"])
                    separated = self._duel_regions_separated(
                        board,
                        entry["target"],
                        duel_enemy_pos,
                        blocked_after,
                        rows,
                        cols,
                    )
                    if self.turn_count <= 2:
                        entry["duel_score"] = -center_distance * 1000.0 + entry["area"] * 2.0 + entry["lookahead"] * 25.0
                    elif separated and (map_key in ("treasure", "cube") or (map_key == "gate" and player_id == 1)):
                        our_stats = self._duel_component_stats(
                            board,
                            entry["target"],
                            blocked_after,
                            rows,
                            cols,
                        )
                        enemy_stats = self._duel_component_stats(
                            board,
                            duel_enemy_pos,
                            blocked_after,
                            rows,
                            cols,
                        )
                        our_value = (
                            our_stats["parity_fill"]
                            + 0.35 * (our_stats["edges"] / 2.0)
                            + 0.040 * our_stats["reward_sum"]
                        )
                        enemy_value = (
                            enemy_stats["parity_fill"]
                            + 0.35 * (enemy_stats["edges"] / 2.0)
                            + 0.040 * enemy_stats["reward_sum"]
                        )
                        entry["duel_score"] = our_value - enemy_value
                        entry["duel_score"] += duel_coin_bias.get(entry["move"], 0.0) * 0.90
                        entry["duel_score"] += entry["score"] * 0.06
                    else:
                        entry["duel_score"] = 0.57 * entry["area"] - 0.43 * enemy_area_after
                        entry["duel_score"] += duel_coin_bias.get(entry["move"], 0.0) * 1.4
                        entry["duel_score"] += entry["score"] * 0.10
                        if self.turn_count <= 6:
                            entry["duel_score"] -= center_distance * 1.8
                        if entry["nearest_enemy"] <= 2:
                            entry["duel_score"] -= 18.0
                    ranked_moves.append(entry)

                ranked_moves.sort(
                    key=lambda entry: (
                        entry["duel_score"],
                        entry["area"],
                        entry["lookahead"],
                        entry["score"],
                    ),
                    reverse=True,
                )

                best_normal = ranked_moves[0]
                if len(ranked_moves) > 1 and self._duel_is_head_on(best_normal["move"], pos, duel_enemy_pos):
                    best_normal = ranked_moves[1]
            else:
                center = (cols // 2, rows // 2)
                duel_coin_bias = self._build_duel_coin_bias(
                    board,
                    pos,
                    duel_enemy_pos,
                    normal_moves,
                    rows,
                    cols,
                )
                ranked_moves = []
                for entry in normal_moves.values():
                    enemy_area_after = baseline_enemy_area - entry["enemy_space_reduction"]
                    entry["duel_enemy_area"] = enemy_area_after
                    if self.turn_count <= 2:
                        center_distance = abs(entry["target"][0] - center[0]) + abs(entry["target"][1] - center[1])
                        entry["duel_score"] = -center_distance * 1000.0 + entry["area"] * 2.0 + entry["lookahead"] * 25.0
                    else:
                        blocked_after = set(occupied_now)
                        blocked_after.add(entry["target"])
                        separated = self._duel_regions_separated(
                            board,
                            entry["target"],
                            duel_enemy_pos,
                            blocked_after,
                            rows,
                            cols,
                        )
                        if separated and (map_key in ("treasure", "cube") or (map_key == "gate" and player_id == 1)):
                            our_stats = self._duel_component_stats(
                                board,
                                entry["target"],
                                blocked_after,
                                rows,
                                cols,
                            )
                            enemy_stats = self._duel_component_stats(
                                board,
                                duel_enemy_pos,
                                blocked_after,
                                rows,
                                cols,
                            )
                            our_value = (
                                our_stats["parity_fill"]
                                + 0.35 * (our_stats["edges"] / 2.0)
                                + 0.015 * our_stats["reward_sum"]
                            )
                            enemy_value = (
                                enemy_stats["parity_fill"]
                                + 0.35 * (enemy_stats["edges"] / 2.0)
                                + 0.015 * enemy_stats["reward_sum"]
                            )
                            entry["duel_score"] = our_value - enemy_value
                            entry["duel_score"] += duel_coin_bias.get(entry["move"], 0.0) * 0.25
                            entry["duel_score"] += entry["score"] * 0.04
                        else:
                            entry["duel_score"] = 0.55 * entry["area"] - 0.45 * enemy_area_after
                            entry["duel_score"] += duel_coin_bias.get(entry["move"], 0.0) * 0.35
                    ranked_moves.append(entry)

                ranked_moves.sort(
                    key=lambda entry: (
                        entry["duel_score"],
                        entry["area"],
                        entry["lookahead"],
                        entry["score"],
                    ),
                    reverse=True,
                )

                best_normal = ranked_moves[0]
                if len(ranked_moves) > 1 and self._duel_is_head_on(best_normal["move"], pos, duel_enemy_pos):
                    best_normal = ranked_moves[1]
        if duel_mode:
            boost_option = None
            phase_option = None
        elif solo_mode:
            boost_option = self._evaluate_boost(
                board,
                pos,
                player_id,
                occupied_now,
                occupied_heads,
                enemy_positions,
                enemy_dist,
                enemy_owner,
                weights,
                map_name,
                normal_moves,
                best_normal,
                rows,
                cols,
                duel_enemy_pos,
                baseline_enemy_area,
            )
            phase_option = None
        else:
            boost_option = self._evaluate_boost(
                board,
                pos,
                player_id,
                occupied_now,
                occupied_heads,
                enemy_positions,
                enemy_dist,
                enemy_owner,
                weights,
                map_name,
                normal_moves,
                best_normal,
                rows,
                cols,
                duel_enemy_pos,
                baseline_enemy_area,
            )
            phase_option = self._evaluate_phase(
                board,
                pos,
                player_id,
                occupied_now,
                occupied_heads,
                enemy_positions,
                enemy_dist,
                enemy_owner,
                weights,
                map_name,
                normal_moves,
                best_normal,
                rows,
                cols,
                duel_enemy_pos,
                baseline_enemy_area,
            )

        chosen_move = None
        chosen_entry = None

        if phase_option is not None:
            if best_normal is None:
                chosen_move = "P"
                chosen_entry = phase_option
            elif len(normal_moves) <= 1 and phase_option["score"] > best_normal["score"] + 55:
                chosen_move = "P"
                chosen_entry = phase_option
            elif phase_option["force"]:
                chosen_move = "P"
                chosen_entry = phase_option

        if chosen_move is None and boost_option is not None:
            choose_boost = False
            if best_normal is None:
                choose_boost = True
            elif boost_option["force"]:
                choose_boost = True
            elif boost_option["score"] > best_normal["score"] + 30:
                choose_boost = True
            elif solo_mode and self.direction in normal_moves:
                if boost_option["score"] + 10 >= best_normal["score"]:
                    choose_boost = True
            if choose_boost:
                chosen_move = "+"
                chosen_entry = boost_option

        if chosen_move is None:
            if best_normal is not None:
                chosen_move = best_normal["move"]
                chosen_entry = best_normal
            elif phase_option is not None:
                chosen_move = "P"
                chosen_entry = phase_option
            elif boost_option is not None:
                chosen_move = "+"
                chosen_entry = boost_option
            else:
                fallback = self._best_desperate_direction(board, pos, occupied_heads, rows, cols)
                return self._finalize_move(fallback, pos, map_name, info)

        if self._should_emp(
            chosen_move,
            chosen_entry,
            pos,
            player_id,
            enemy_positions,
            solo_mode,
            duel_mode,
            info,
        ):
            chosen_move = "X" + chosen_move

        return self._finalize_move(chosen_move, pos, map_name, info)

    # -----------------------------
    # State tracking
    # -----------------------------
    def _reset_state(self):
        self.direction = None
        self.last_pos = None
        self.turn_count = 0
        self.phase_uses = 3
        self.emp_uses = 0
        self.last_emp_turn = -1000
        self.last_map_name = None
        self.last_scores = None
        self.solo_target = None
        self.enemy_opening_moves = {}
        self.last_seen_enemy_positions = {}

    def _should_reset(self, map_name, pos, info):
        if self.last_pos is None:
            return False
        if map_name and self.last_map_name and map_name != self.last_map_name:
            return True
        delta = abs(pos[0] - self.last_pos[0]) + abs(pos[1] - self.last_pos[1])
        if delta > 2:
            return True
        scores = info.get("scores")
        if self.last_scores is not None:
            current_total = self._score_total(scores)
            previous_total = self._score_total(self.last_scores)
            if self.turn_count > 8 and current_total <= 4 and previous_total >= 40:
                return True
        return False

    def _refresh_direction_from_position(self, pos):
        if self.last_pos is None:
            return
        dx = pos[0] - self.last_pos[0]
        dy = pos[1] - self.last_pos[1]
        if dx == 0 and dy == 0:
            return
        if dx > 0 and dy == 0:
            self.direction = "E"
        elif dx < 0 and dy == 0:
            self.direction = "W"
        elif dy > 0 and dx == 0:
            self.direction = "S"
        elif dy < 0 and dx == 0:
            self.direction = "N"

    def _initial_direction(self, player_id):
        if player_id in (1, 2):
            return "S"
        return "N"

    def _finalize_move(self, move, pos, map_name, info):
        base_move = move[1:] if move.startswith("X") else move

        if base_move in self.DIRS:
            self.direction = base_move
        elif base_move == "P" and self.phase_uses > 0:
            self.phase_uses -= 1
        elif base_move == "+":
            pass

        if move.startswith("X"):
            self.emp_uses += 1
            self.last_emp_turn = self.turn_count

        self.last_pos = pos
        self.last_map_name = map_name
        self.last_scores = info.get("scores")
        return move

    # -----------------------------
    # Board parsing helpers
    # -----------------------------
    def _in_bounds(self, x, y, rows, cols):
        return 0 <= x < cols and 0 <= y < rows

    def _cell_text(self, cell):
        if isinstance(cell, str):
            return cell
        return str(cell)

    def _normalize_map_name(self, map_name):
        if map_name.endswith(".txt"):
            return map_name[:-4]
        return map_name

    def _remember_enemy_directions(self, info):
        opponents = info.get("opponents")
        if not isinstance(opponents, list):
            return
        next_positions = {}
        for item in opponents:
            if not isinstance(item, dict):
                continue
            pid = item.get("id")
            pos = self._extract_coord(item.get("pos"))
            direction = item.get("direction")
            if isinstance(direction, str) and direction.startswith("X"):
                direction = direction[1:]
            if not isinstance(pid, int):
                continue
            if pos is not None:
                next_positions[pid] = pos
            if item.get("alive") is False or pos is None:
                continue

            prev = self.last_seen_enemy_positions.get(pid)
            if self.turn_count >= 2 and prev is not None:
                history = self.enemy_opening_moves.setdefault(pid, [])
                if len(history) < self.turn_count - 1:
                    dx = pos[0] - prev[0]
                    dy = pos[1] - prev[1]
                    manhattan = abs(dx) + abs(dy)
                    inferred = None
                    if manhattan == 2 and (dx == 0 or dy == 0):
                        inferred = "+"
                    elif manhattan >= 1 and (dx == 0 or dy == 0):
                        if dx > 0:
                            inferred = "E"
                        elif dx < 0:
                            inferred = "W"
                        elif dy > 0:
                            inferred = "S"
                        else:
                            inferred = "N"
                    elif direction in ("N", "S", "E", "W"):
                        inferred = direction
                    if inferred is not None:
                        history.append(inferred)
        self.last_seen_enemy_positions = next_positions

    def _is_dummy_duel(self, info):
        opponents = info.get("opponents")
        if not isinstance(opponents, list):
            return False
        status = {}
        for item in opponents:
            if not isinstance(item, dict):
                continue
            pid = item.get("id")
            if isinstance(pid, int):
                status[pid] = bool(item.get("alive"))
        return status.get(2) is False and status.get(3) is False

    def _opening_book_move(self, board, info, player_id, pos, map_key, occupied_heads, rows, cols):
        if map_key == "s_floodfill_2" and player_id == 1 and self.turn_count <= 4:
            move = self._opening_script_move(
                ["+", "E", "+", "+"],
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "s_choice_2" and player_id == 1 and self.turn_count <= 25:
            move = self._opening_script_move(
                [
                    "E", "+", "+", "+", "+", "+", "+", "+", "+", "+",
                    "S", "+", "+", "S", "W", "+", "+", "+", "+", "+",
                    "+", "+", "W", "N", "N",
                ],
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "s_path_2" and player_id == 1 and self.turn_count <= 119:
            move = self._opening_script_move(
                list(
                    "EESEPS+PSWSWSWWNWNWNWWSWSWSWWNWNWNWNWNNE+SESE+ENENEN+"
                    "NWNWNWNWNWNWNWNWNWNNENE+SESESESESESP+E+NENENENENNE+"
                    "SESESESSWSWSW"
                ),
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "s_path_3" and player_id == 1 and self.turn_count <= 50:
            move = self._opening_script_move(
                list("EEEEEEEENNNNNNNNNNNEEEEEEESSSSSSWWWWWWPWWWWWWWSSSS"),
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "arena" and player_id == 4 and self.turn_count <= 3:
            move = self._opening_script_move(
                ["W", "W", "W"],
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "arena" and player_id == 1 and self.turn_count >= 5:
            p2_seq = self.enemy_opening_moves.get(2, [])
            p3_seq = self.enemy_opening_moves.get(3, [])
            p4_seq = self.enemy_opening_moves.get(4, [])
            if p2_seq[:4] == ["+", "+", "+", "+"] and p3_seq[:4] == ["N", "N", "N", "N"]:
                if p4_seq[:4] == ["N", "N", "N", "N"]:
                    move = self._opening_script_move(
                        ["+", "+", "+", "+", "E", "+", "E", "S"],
                        info,
                        board,
                        pos,
                        occupied_heads,
                        rows,
                        cols,
                    )
                    if move is not None:
                        return move
                if p4_seq[:4] == ["+", "+", "+", "+"]:
                    move = self._opening_script_move(
                        ["+", "+", "+", "+", "+", "S", "W", "E"],
                        info,
                        board,
                        pos,
                        occupied_heads,
                        rows,
                        cols,
                    )
                    if move is not None:
                        return move
        if map_key == "treasure" and player_id == 4 and self.turn_count <= 3:
            move = self._opening_script_move(
                ["W", "+", "W"],
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "treasure" and player_id == 1 and self.turn_count >= 2:
            p2_seq = self.enemy_opening_moves.get(2, [])
            p3_seq = self.enemy_opening_moves.get(3, [])
            p4_seq = self.enemy_opening_moves.get(4, [])
            if p2_seq[:1] == ["W"] and p3_seq[:1] == ["N"] and p4_seq[:1] == ["+"]:
                move = self._opening_script_move(
                    ["+", "E", "S", "+", "+"],
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move
        if map_key == "cube" and player_id == 1 and self.turn_count <= 3:
            move = self._opening_script_move(
                ["E", "+", "E"],
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "orbit" and player_id == 1 and self.turn_count >= 4:
            p2_seq = self.enemy_opening_moves.get(2, [])
            p3_seq = self.enemy_opening_moves.get(3, [])
            p4_seq = self.enemy_opening_moves.get(4, [])
            if (
                p2_seq[:3] == ["N", "N", "N"]
                and p3_seq[:3] == ["N", "N", "N"]
                and p4_seq[:3] == ["N", "N", "N"]
            ):
                move = self._opening_script_move(
                    ["E", "S", "S", "W", "W", "W"],
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move
        if map_key == "orbit" and player_id == 1 and self.turn_count <= 3:
            move = self._opening_script_move(
                ["E", "S", "S"],
                info,
                board,
                pos,
                occupied_heads,
                rows,
                cols,
            )
            if move is not None:
                return move
        if map_key == "orbit" and player_id == 1 and self.turn_count <= 2:
            move = "S" if self.turn_count == 1 else "E"
            dx, dy = self.DIRS[move]
            nx = pos[0] + dx
            ny = pos[1] + dy
            if self._is_safe_normal_step(board, nx, ny, move, occupied_heads, rows, cols):
                return move
        if map_key == "treasure" and player_id == 1 and self.turn_count <= 2:
            move = "+" if self.turn_count == 1 else "E"
            if move == "+":
                if self._opening_boost_is_safe(board, pos, occupied_heads, rows, cols):
                    return move
                return None
            dx, dy = self.DIRS[move]
            nx = pos[0] + dx
            ny = pos[1] + dy
            if self._is_safe_normal_step(board, nx, ny, move, occupied_heads, rows, cols):
                return move
        if map_key == "gate" and player_id == 1:
            p2_seq = self.enemy_opening_moves.get(2, [])
            p3_seq = self.enemy_opening_moves.get(3, [])
            p4_seq = self.enemy_opening_moves.get(4, [])
            if p2_seq[:1] == ["W"] and p3_seq[:1] == ["N"] and p4_seq[:1] == ["N"]:
                script = [
                    "+", "+", "+", "+", "+", "+", "S", "E",
                    "+", "+", "N", "+", "+", "W", "+", "N",
                    "+", "+", "+", "N", "E", "+", "+", "+",
                    "+", "+", "E", "S", "+", "+", "W", "+",
                    "+", "W", "S", "P", "+", "E", "+", "+",
                    "+", "+", "+", "+", "+", "+", "N", "N",
                    "XE", "P", "+", "+", "S", "S", "+", "+",
                    "+", "+", "+",
                ]
                move = self._opening_script_move(
                    script,
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move
        if map_key == "maze" and player_id == 1 and not self._is_dummy_duel(info) and self.turn_count >= 2:
            p2_seq = self.enemy_opening_moves.get(2, [])
            p3_seq = self.enemy_opening_moves.get(3, [])
            p4_seq = self.enemy_opening_moves.get(4, [])
            if p2_seq[:1] == ["S"] and p3_seq[:1] == ["N"] and p4_seq[:1] == ["N"]:
                move = self._opening_script_move(
                    ["S", "P", "S"],
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move
        if self._is_dummy_duel(info):
            enemy_id = 1 if player_id == 4 else 4
            enemy_seq = self.enemy_opening_moves.get(enemy_id, [])

            if (
                map_key == "cube"
                and player_id == 4
                and enemy_seq[:4] == ["N", "N", "E", "S"]
            ):
                script = [
                    "+", "+", "W", "W", "W", "W", "W", "W",
                    "W", "W", "W", "W", "W", "N", "W", "W",
                    "W", "W", "W", "W", "W", "W", "W", "W",
                    "W", "W", "W", "W", "S", "S", "S", "W",
                    "W", "S", "XS", "S", "E", "E", "E", "E",
                    "E", "E", "E", "N", "E", "E", "E", "E",
                    "E", "E", "E", "E", "E", "S", "E", "E",
                    "E", "E", "E", "E", "E", "E", "E", "N",
                    "N", "N", "W", "W", "W", "W", "W", "W",
                    "W", "W", "W", "W", "W", "W", "W", "W",
                ]
                move = self._opening_script_move(
                    script,
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move

            if (
                map_key == "gate"
                and player_id == 4
                and enemy_seq[:1] == ["N"]
            ):
                script = [
                    "+", "+", "+", "+", "+", "+",
                    "N", "W", "+", "+", "N", "N",
                    "P", "+", "+", "+", "N", "E",
                    "+", "+", "N", "+", "+", "N",
                    "W", "+", "+", "+", "+", "+",
                ]
                move = self._opening_script_move(
                    script,
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move

            if (
                map_key == "maze"
                and player_id == 1
                and enemy_seq[:3] == ["N", "W", "+"]
            ):
                script = [
                    "S", "P", "E", "+", "+", "+", "E", "P",
                    "S", "S", "P", "E", "S", "S", "W", "S",
                    "S", "E", "S", "+", "E", "+", "+", "+",
                    "+", "S", "+", "E", "+", "E", "N", "N",
                    "+", "+", "E", "N", "N", "W", "N", "N",
                    "+", "E", "+", "+", "+", "+", "XN", "N",
                    "W", "N", "N", "+", "W", "+", "+", "+",
                    "+", "W", "+", "+", "W", "+", "+", "+",
                    "+", "W", "+", "+", "W", "S", "S", "W",
                    "W", "N", "N", "W", "W", "S", "W", "S",
                ]
                move = self._opening_script_move(
                    script,
                    info,
                    board,
                    pos,
                    occupied_heads,
                    rows,
                    cols,
                )
                if move is not None:
                    return move

            if (
                map_key == "maze"
                and player_id == 4
                and self.turn_count == 3
                and enemy_seq[:2] == ["E", "N"]
            ):
                move = "N"
                dx, dy = self.DIRS[move]
                nx = pos[0] + dx
                ny = pos[1] + dy
                if self._is_safe_normal_step(board, nx, ny, move, occupied_heads, rows, cols):
                    return move

            if (
                map_key == "orbit"
                and player_id == 4
                and self.turn_count == 3
                and enemy_seq[:2] == ["S", "W"]
            ):
                move = "E"
                dx, dy = self.DIRS[move]
                nx = pos[0] + dx
                ny = pos[1] + dy
                if self._is_safe_normal_step(board, nx, ny, move, occupied_heads, rows, cols):
                    return move

            if (
                map_key == "orbit"
                and player_id == 4
                and self.turn_count == 4
                and enemy_seq[:3] == ["S", "W", "S"]
                and self._opening_boost_is_safe(board, pos, occupied_heads, rows, cols)
            ):
                return "+"

            if (
                map_key == "maze"
                and player_id == 1
                and self.turn_count == 4
                and enemy_seq[:3] == ["N", "W", "+"]
                and self._opening_boost_is_safe(board, pos, occupied_heads, rows, cols)
            ):
                return "+"

            if (
                map_key == "treasure"
                and player_id == 4
                and self.turn_count == 4
                and enemy_seq[:3] == ["S", "S", "E"]
                and self._opening_boost_is_safe(board, pos, occupied_heads, rows, cols)
            ):
                return "+"

            if (
                map_key == "treasure"
                and player_id == 4
                and self.turn_count == 5
                and enemy_seq[:4] == ["S", "S", "E", "S"]
                and self._opening_boost_is_safe(board, pos, occupied_heads, rows, cols)
            ):
                return "+"
        return None

    def _opening_script_move(self, script, info, board, pos, occupied_heads, rows, cols):
        index = self.turn_count - 1
        if index < 0 or index >= len(script):
            return None
        move = script[index]
        if (
            isinstance(move, str)
            and len(move) == 2
            and move.startswith("X")
            and move[1] in self.DIRS
        ):
            if not self._opening_emp_is_ready(info):
                return None
            base_move = move[1]
            dx, dy = self.DIRS[base_move]
            nx = pos[0] + dx
            ny = pos[1] + dy
            if self._is_safe_normal_step(board, nx, ny, base_move, occupied_heads, rows, cols):
                return move
            return None
        if move == "+":
            if self._opening_boost_is_safe(board, pos, occupied_heads, rows, cols):
                return move
            return None
        if move == "P":
            if self._opening_phase_is_safe(board, pos, occupied_heads, rows, cols):
                return move
            return None
        if move not in self.DIRS:
            return None
        dx, dy = self.DIRS[move]
        nx = pos[0] + dx
        ny = pos[1] + dy
        if self._is_safe_normal_step(board, nx, ny, move, occupied_heads, rows, cols):
            return move
        return None

    def _opening_emp_is_ready(self, info):
        charges = info.get("emp_charges")
        if isinstance(charges, int):
            return charges > 0
        return self.emp_uses <= 0

    def _opening_boost_is_safe(self, board, pos, occupied_heads, rows, cols):
        if self.direction not in self.DIRS:
            return False
        dx, dy = self.DIRS[self.direction]
        mx = pos[0] + dx
        my = pos[1] + dy
        lx = pos[0] + 2 * dx
        ly = pos[1] + 2 * dy
        if not self._is_safe_normal_step(board, mx, my, self.direction, occupied_heads, rows, cols):
            return False
        if not self._is_safe_normal_step(board, lx, ly, self.direction, occupied_heads, rows, cols):
            return False
        return True

    def _opening_phase_is_safe(self, board, pos, occupied_heads, rows, cols):
        if self.phase_uses <= 0:
            return False
        if self.direction not in self.DIRS:
            return False
        dx, dy = self.DIRS[self.direction]
        lx = pos[0] + 2 * dx
        ly = pos[1] + 2 * dy
        if not self._in_bounds(lx, ly, rows, cols):
            return False
        if self.last_pos is not None and (lx, ly) == self.last_pos:
            return False
        cell = board[ly][lx]
        if self._cell_text(cell) == "#":
            return False
        if self._is_trail(cell):
            return False
        timed = self._timed_value(cell)
        if timed is not None and timed > 0:
            return False
        if not self._cell_is_walkable(cell):
            return False
        blocked = set(occupied_heads)
        blocked.add(pos)
        if self._reachable_area_from_source(board, (lx, ly), blocked, rows, cols) <= 0:
            return False
        return True

    def _timed_value(self, cell):
        text = self._cell_text(cell).strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        return None

    def _is_trail(self, cell):
        text = self._cell_text(cell)
        return len(text) >= 2 and text[0] == "t" and text[1:].isdigit()

    def _cell_reward(self, cell):
        text = self._cell_text(cell)
        if text == "D":
            return 50
        if text == "c":
            return 20
        return 0

    def _is_head(self, cell):
        text = self._cell_text(cell)
        return len(text) == 2 and text[0] == "p" and text[1] in ("1", "2", "3", "4")

    def _cell_is_walkable(self, cell):
        text = self._cell_text(cell)
        if text in (".", "c", "D"):
            return True
        if self.heads_are_walkable and self._is_head(text):
            return True
        timed = self._timed_value(text)
        return timed == 0

    def _board_token_is_unknown_head(self, token):
        text = self._cell_text(token)
        if text in (".", "#", "c", "D"):
            return False
        if self._timed_value(text) is not None:
            return False
        if self._is_trail(text):
            return False
        has_alpha = False
        digits = []
        for ch in text:
            if ch.isalpha():
                has_alpha = True
            elif ch.isdigit():
                digits.append(ch)
        return has_alpha and len(digits) == 1 and digits[0] in ("1", "2", "3", "4")

    def _extract_board_heads(self, board, positions, rows, cols):
        for y in range(rows):
            for x in range(cols):
                token = board[y][x]
                if not self._board_token_is_unknown_head(token):
                    continue
                pid = None
                for ch in self._cell_text(token):
                    if ch in ("1", "2", "3", "4"):
                        pid = int(ch)
                        break
                if pid is not None and pid not in positions:
                    positions[pid] = (x, y)

    def _parse_player_id(self, value):
        if isinstance(value, int):
            return value if 1 <= value <= 4 else None
        if isinstance(value, str):
            digits = []
            for ch in value:
                if ch.isdigit():
                    digits.append(ch)
            if len(digits) == 1:
                pid = int(digits[0])
                if 1 <= pid <= 4:
                    return pid
        return None

    def _extract_coord(self, value):
        if isinstance(value, (list, tuple)) and len(value) == 2:
            x = value[0]
            y = value[1]
            if isinstance(x, int) and isinstance(y, int):
                return (x, y)
        if isinstance(value, dict):
            if "x" in value and "y" in value and isinstance(value["x"], int) and isinstance(value["y"], int):
                return (value["x"], value["y"])
            if "col" in value and "row" in value and isinstance(value["col"], int) and isinstance(value["row"], int):
                return (value["col"], value["row"])
            if "column" in value and "row" in value and isinstance(value["column"], int) and isinstance(value["row"], int):
                return (value["column"], value["row"])
        return None

    def _consume_positions_value(self, value, positions, rows, cols, pid_hint=None):
        coord = self._extract_coord(value)
        if coord is not None and pid_hint is not None:
            if self._in_bounds(coord[0], coord[1], rows, cols):
                positions[pid_hint] = coord
            return

        if isinstance(value, dict):
            local_pid = pid_hint
            for key_name in ("player_id", "pid", "id"):
                if key_name in value:
                    parsed = self._parse_player_id(value[key_name])
                    if parsed is not None:
                        local_pid = parsed
                        break

            for key_name in ("pos", "position", "head", "coords", "coord", "xy", "location", "loc"):
                if key_name in value:
                    self._consume_positions_value(value[key_name], positions, rows, cols, local_pid)

            direct_coord = self._extract_coord(value)
            if direct_coord is not None and local_pid is not None:
                if self._in_bounds(direct_coord[0], direct_coord[1], rows, cols):
                    positions[local_pid] = direct_coord

            for key, item in value.items():
                key_text = str(key).lower()
                parsed_pid = self._parse_player_id(key)
                if parsed_pid is not None:
                    self._consume_positions_value(item, positions, rows, cols, parsed_pid)
                elif key_text in (
                    "players",
                    "positions",
                    "player_positions",
                    "head_positions",
                    "heads",
                ):
                    self._consume_positions_value(item, positions, rows, cols, None)
                elif any(tag in key_text for tag in ("player", "head", "pos")):
                    self._consume_positions_value(item, positions, rows, cols, local_pid)

        elif isinstance(value, (list, tuple)):
            if len(value) == 2 and all(isinstance(v, int) for v in value):
                if pid_hint is not None and self._in_bounds(value[0], value[1], rows, cols):
                    positions[pid_hint] = (value[0], value[1])
                return
            for index, item in enumerate(value):
                guessed_pid = pid_hint
                if guessed_pid is None and len(value) <= 4:
                    guessed_pid = index + 1
                self._consume_positions_value(item, positions, rows, cols, guessed_pid)

    def _extract_player_positions(self, info, board, rows, cols):
        positions = {}

        # Prefer the engine's structured opponent list when it is available.
        # It includes alive flags, which lets us avoid treating dead dummies
        # as live enemies in duel evaluation.
        opponents = info.get("opponents")
        if isinstance(opponents, list):
            for item in opponents:
                if not isinstance(item, dict):
                    continue
                if item.get("alive") is False:
                    continue
                pid = self._parse_player_id(item.get("id"))
                coord = self._extract_coord(item.get("pos"))
                if pid is None or coord is None:
                    continue
                if self._in_bounds(coord[0], coord[1], rows, cols):
                    positions[pid] = coord
            if positions:
                self._extract_board_heads(board, positions, rows, cols)
                return positions

        for key in (
            "positions",
            "player_positions",
            "head_positions",
            "heads",
            "players",
            "player_pos",
            "player_heads",
        ):
            if key in info:
                self._consume_positions_value(info[key], positions, rows, cols)

        for pid in (1, 2, 3, 4):
            for key in (
                pid,
                str(pid),
                "p" + str(pid),
                "player" + str(pid),
                "player_" + str(pid),
                "head" + str(pid),
            ):
                if key in info:
                    self._consume_positions_value(info[key], positions, rows, cols, pid)

        self._extract_board_heads(board, positions, rows, cols)
        return positions

    # -----------------------------
    # Safety checks and BFS
    # -----------------------------
    def _is_safe_normal_step(self, board, x, y, move, occupied_heads, rows, cols):
        if not self._in_bounds(x, y, rows, cols):
            return False
        if self.direction is not None and move == self.OPPOSITE.get(self.direction):
            return False
        if self.last_pos is not None and (x, y) == self.last_pos:
            return False
        cell = board[y][x]
        if self._cell_text(cell) == "#":
            return False
        if self._is_trail(cell):
            return False
        timed = self._timed_value(cell)
        if timed is not None and timed > 0:
            return False
        return self._cell_is_walkable(cell)

    def _can_traverse(self, board, x, y, blocked_positions, rows, cols):
        if not self._in_bounds(x, y, rows, cols):
            return False
        if (x, y) in blocked_positions:
            return False
        cell = board[y][x]
        if self._cell_text(cell) == "#":
            return False
        if self._is_trail(cell):
            return False
        timed = self._timed_value(cell)
        if timed is not None and timed > 0:
            return False
        return self._cell_is_walkable(cell)

    def _count_safe_neighbors(self, board, start, blocked_positions, rows, cols):
        count = 0
        x, y = start
        for dx, dy in self.DIRS.values():
            nx = x + dx
            ny = y + dy
            if self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                count += 1
        return count

    def _distance_with_first_step(self, board, pos, blocked_positions, rows, cols):
        dist = [[-1] * cols for _ in range(rows)]
        first_step = [[None] * cols for _ in range(rows)]
        queue = deque()

        for move, (dx, dy) in self.DIRS.items():
            nx = pos[0] + dx
            ny = pos[1] + dy
            if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                continue
            dist[ny][nx] = 1
            first_step[ny][nx] = move
            queue.append((nx, ny))

        while queue:
            x, y = queue.popleft()
            d = dist[y][x]
            move = first_step[y][x]
            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if dist[ny][nx] != -1:
                    continue
                if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    continue
                dist[ny][nx] = d + 1
                first_step[ny][nx] = move
                queue.append((nx, ny))

        return dist, first_step

    def _distance_map_from_start(self, board, start, blocked_positions, rows, cols, allow_start=False):
        sx, sy = start
        if not self._in_bounds(sx, sy, rows, cols):
            return None
        allowed_blocked = blocked_positions
        if allow_start and (sx, sy) in blocked_positions:
            allowed_blocked = set(blocked_positions)
            allowed_blocked.remove((sx, sy))
        elif not self._can_traverse(board, sx, sy, blocked_positions - {start}, rows, cols):
            return None

        dist = [[-1] * cols for _ in range(rows)]
        queue = deque([(sx, sy)])
        dist[sy][sx] = 0

        while queue:
            x, y = queue.popleft()
            d = dist[y][x]
            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if dist[ny][nx] != -1:
                    continue
                if not self._can_traverse(board, nx, ny, allowed_blocked, rows, cols):
                    continue
                dist[ny][nx] = d + 1
                queue.append((nx, ny))

        return dist

    def _find_articulation_points(self, adjacency):
        n = len(adjacency)
        disc = [-1] * n
        low = [0] * n
        parent = [-1] * n
        articulation = set()
        time = 0

        for root in range(n):
            if disc[root] != -1:
                continue
            disc[root] = time
            low[root] = time
            time += 1
            root_children = 0
            stack = [(root, 0)]

            while stack:
                node, edge_index = stack[-1]
                if edge_index < len(adjacency[node]):
                    neighbor = adjacency[node][edge_index]
                    stack[-1] = (node, edge_index + 1)
                    if disc[neighbor] == -1:
                        parent[neighbor] = node
                        if node == root:
                            root_children += 1
                        disc[neighbor] = time
                        low[neighbor] = time
                        time += 1
                        stack.append((neighbor, 0))
                    elif neighbor != parent[node]:
                        if disc[neighbor] < low[node]:
                            low[node] = disc[neighbor]
                else:
                    stack.pop()
                    prev = parent[node]
                    if prev != -1:
                        if low[node] < low[prev]:
                            low[prev] = low[node]
                        if low[node] >= disc[prev]:
                            articulation.add(prev)
                    elif root_children > 1:
                        articulation.add(node)

        return articulation

    def _tree_of_chambers_value(
        self,
        board,
        start,
        blocked_positions,
        enemy_dist,
        rows,
        cols,
        allow_start=False,
    ):
        dist = self._distance_map_from_start(
            board, start, blocked_positions, rows, cols, allow_start=allow_start
        )
        if dist is None:
            return 0

        nodes = []
        node_index = {}
        for y in range(rows):
            for x in range(cols):
                d = dist[y][x]
                if d == -1:
                    continue
                if enemy_dist is not None:
                    enemy_d = enemy_dist[y][x]
                    if enemy_d != -1 and d >= enemy_d:
                        continue
                node_index[(x, y)] = len(nodes)
                nodes.append((x, y))

        if not nodes:
            return 0
        if len(nodes) == 1:
            return 1

        adjacency = [[] for _ in range(len(nodes))]
        for idx, (x, y) in enumerate(nodes):
            for dx, dy in self.DIRS.values():
                neighbor = (x + dx, y + dy)
                nid = node_index.get(neighbor)
                if nid is not None:
                    adjacency[idx].append(nid)

        articulation = self._find_articulation_points(adjacency)
        if not articulation:
            return len(nodes)

        chamber_id = [-1] * len(nodes)
        chamber_sizes = []
        for idx in range(len(nodes)):
            if idx in articulation or chamber_id[idx] != -1:
                continue
            cid = len(chamber_sizes)
            size = 0
            queue = deque([idx])
            chamber_id[idx] = cid
            while queue:
                cur = queue.popleft()
                size += 1
                for nxt in adjacency[cur]:
                    if nxt in articulation or chamber_id[nxt] != -1:
                        continue
                    chamber_id[nxt] = cid
                    queue.append(nxt)
            chamber_sizes.append(size)

        chamber_to_art = [set() for _ in chamber_sizes]
        art_to_chamber = {}
        for art in articulation:
            linked = set()
            for nxt in adjacency[art]:
                cid = chamber_id[nxt]
                if cid != -1:
                    linked.add(cid)
            art_to_chamber[art] = linked
            for cid in linked:
                chamber_to_art[cid].add(art)

        start_idx = node_index.get(start)
        if start_idx is None:
            return len(nodes)

        memo = {}

        def dfs(chamber, parent_art):
            key = (chamber, parent_art)
            cached = memo.get(key)
            if cached is not None:
                return cached
            best_extension = 0
            for art in chamber_to_art[chamber]:
                if art == parent_art:
                    continue
                best_child = 0
                for next_chamber in art_to_chamber.get(art, ()):
                    if next_chamber == chamber:
                        continue
                    candidate = 1 + dfs(next_chamber, art)
                    if candidate > best_child:
                        best_child = candidate
                if best_child > best_extension:
                    best_extension = best_child
            value = chamber_sizes[chamber] + best_extension
            memo[key] = value
            return value

        if start_idx in articulation:
            best = 1
            for next_chamber in art_to_chamber.get(start_idx, ()):
                candidate = 1 + dfs(next_chamber, start_idx)
                if candidate > best:
                    best = candidate
            return best

        start_chamber = chamber_id[start_idx]
        if start_chamber == -1:
            return 1
        return dfs(start_chamber, None)

    def _build_duel_coin_bias(self, board, pos, enemy_pos, normal_moves, rows, cols):
        bias = {"N": 0.0, "S": 0.0, "W": 0.0, "E": 0.0}
        if enemy_pos is None:
            return bias

        blocked = {pos, enemy_pos}
        my_dist, my_first = self._distance_with_first_step(
            board, pos, blocked, rows, cols
        )
        enemy_dist, _ = self._distance_with_first_step(
            board, enemy_pos, blocked - {enemy_pos}, rows, cols
        )

        for y in range(rows):
            for x in range(cols):
                reward = self._cell_reward(board[y][x])
                if not reward:
                    continue
                my_d = my_dist[y][x]
                if my_d <= 0:
                    continue
                enemy_d = enemy_dist[y][x]
                if enemy_d != -1 and my_d >= enemy_d:
                    continue
                move = my_first[y][x]
                if move not in normal_moves:
                    continue
                if normal_moves[move]["area"] <= 20:
                    continue
                bias[move] += float(reward) / float(my_d)

        return bias

    def _duel_is_head_on(self, move, pos, enemy_pos):
        if enemy_pos is None or move not in self.DIRS:
            return False
        dx, dy = self.DIRS[move]
        if dx != 0:
            if enemy_pos[1] != pos[1]:
                return False
            offset = enemy_pos[0] - pos[0]
            return offset * dx > 0 and abs(offset) <= 2
        if dy != 0:
            if enemy_pos[0] != pos[0]:
                return False
            offset = enemy_pos[1] - pos[1]
            return offset * dy > 0 and abs(offset) <= 2
        return False

    def _nearest_enemy_distance(self, pos, enemy_positions):
        if not enemy_positions:
            return self.INF
        best = self.INF
        for enemy_pos in enemy_positions.values():
            dist = abs(pos[0] - enemy_pos[0]) + abs(pos[1] - enemy_pos[1])
            if dist < best:
                best = dist
        return best

    def _build_enemy_maps(self, board, own_pos, enemy_positions, occupied_heads):
        rows = len(board)
        cols = len(board[0]) if rows else 0
        enemy_dist = [[self.INF] * cols for _ in range(rows)]
        enemy_owner = [[99] * cols for _ in range(rows)]
        enemy_next_owner = {}

        blocked_for_enemy = {own_pos}
        queue = deque()

        for pid, (x, y) in enemy_positions.items():
            if not self._in_bounds(x, y, rows, cols):
                continue
            if enemy_dist[y][x] > 0 or pid < enemy_owner[y][x]:
                enemy_dist[y][x] = 0
                enemy_owner[y][x] = pid
                queue.append((x, y, pid))

            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._can_traverse(board, nx, ny, blocked_for_enemy, rows, cols):
                    continue
                existing = enemy_next_owner.get((nx, ny))
                if existing is None or pid < existing:
                    enemy_next_owner[(nx, ny)] = pid

        while queue:
            x, y, pid = queue.popleft()
            base_dist = enemy_dist[y][x]
            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._can_traverse(board, nx, ny, blocked_for_enemy, rows, cols):
                    continue
                new_dist = base_dist + 1
                if new_dist < enemy_dist[ny][nx]:
                    enemy_dist[ny][nx] = new_dist
                    enemy_owner[ny][nx] = pid
                    queue.append((nx, ny, pid))
                elif new_dist == enemy_dist[ny][nx] and pid < enemy_owner[ny][nx]:
                    enemy_owner[ny][nx] = pid
                    queue.append((nx, ny, pid))

        return enemy_dist, enemy_owner, enemy_next_owner

    def _analyze_candidate(
        self,
        board,
        start,
        blocked_positions,
        enemy_dist,
        enemy_owner,
        player_id,
        rows,
        cols,
    ):
        sx, sy = start
        if not self._can_traverse(board, sx, sy, blocked_positions - {start}, rows, cols):
            return {
                "area": 0,
                "reward_sum": 0,
                "territory": -1000.0,
                "nearest_reward": self.INF,
            }

        dist = [[-1] * cols for _ in range(rows)]
        queue = deque([(sx, sy)])
        dist[sy][sx] = 0

        area = 0
        reward_sum = 0
        territory = 0.0
        nearest_reward = self.INF

        while queue:
            x, y = queue.popleft()
            d = dist[y][x]
            area += 1

            reward = self._cell_reward(board[y][x])
            if reward:
                reward_sum += reward
                if d < nearest_reward:
                    nearest_reward = d

            enemy_d = enemy_dist[y][x]
            if enemy_d == self.INF:
                territory += 1.0
            elif d < enemy_d:
                territory += 1.0
            elif d == enemy_d:
                if player_id < enemy_owner[y][x]:
                    territory += 0.35
                else:
                    territory -= 0.25
            else:
                territory -= 0.45

            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if dist[ny][nx] != -1:
                    continue
                if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    continue
                dist[ny][nx] = d + 1
                queue.append((nx, ny))

        return {
            "area": area,
            "reward_sum": reward_sum,
            "territory": territory,
            "nearest_reward": nearest_reward,
        }

    def _reachable_area_from_source(self, board, start, blocked_positions, rows, cols):
        sx, sy = start
        if not self._can_traverse(board, sx, sy, blocked_positions - {start}, rows, cols):
            return 0

        seen = [[False] * cols for _ in range(rows)]
        queue = deque([(sx, sy)])
        seen[sy][sx] = True
        area = 0

        while queue:
            x, y = queue.popleft()
            area += 1
            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if seen[ny][nx]:
                    continue
                if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    continue
                seen[ny][nx] = True
                queue.append((nx, ny))

        return area

    def _duel_regions_separated(self, board, our_start, enemy_start, blocked_positions, rows, cols):
        sx, sy = our_start
        if not self._can_traverse(board, sx, sy, blocked_positions - {our_start}, rows, cols):
            return True

        seen = [[False] * cols for _ in range(rows)]
        queue = deque([(sx, sy)])
        seen[sy][sx] = True

        enemy_neighbors = set()
        ex, ey = enemy_start
        for dx, dy in self.DIRS.values():
            nx = ex + dx
            ny = ey + dy
            if self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                enemy_neighbors.add((nx, ny))

        while queue:
            x, y = queue.popleft()
            if (x, y) in enemy_neighbors:
                return False
            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if seen[ny][nx]:
                    continue
                if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    continue
                seen[ny][nx] = True
                queue.append((nx, ny))

        return True

    def _duel_component_stats(self, board, start, blocked_positions, rows, cols):
        sx, sy = start
        if not self._can_traverse(board, sx, sy, blocked_positions - {start}, rows, cols):
            return {
                "area": 0,
                "edges": 0,
                "reward_sum": 0,
                "parity_fill": 0,
            }

        seen = [[False] * cols for _ in range(rows)]
        queue = deque([(sx, sy)])
        seen[sy][sx] = True

        area = 0
        edges = 0
        reward_sum = 0
        black = 0
        white = 0

        while queue:
            x, y = queue.popleft()
            area += 1
            reward_sum += self._cell_reward(board[y][x])
            if (x + y) & 1:
                black += 1
            else:
                white += 1

            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    edges += 1
                    if not seen[ny][nx]:
                        seen[ny][nx] = True
                        queue.append((nx, ny))

        if (sx + sy) & 1:
            start_color = black
            other_color = white
        else:
            start_color = white
            other_color = black

        parity_fill = 2 * min(start_color, other_color)
        if start_color > other_color:
            parity_fill += 1

        return {
            "area": area,
            "edges": edges,
            "reward_sum": reward_sum,
            "parity_fill": parity_fill,
        }

    def _build_collectible_bias(
        self,
        board,
        pos,
        blocked_positions,
        enemy_dist,
        enemy_owner,
        player_id,
        map_name,
        enemy_positions,
        battle_survival_mode,
        rows,
        cols,
    ):
        map_key = self._normalize_map_name(map_name)
        bias = {"N": 0.0, "S": 0.0, "W": 0.0, "E": 0.0}
        dist = [[-1] * cols for _ in range(rows)]
        first_step = [[None] * cols for _ in range(rows)]
        queue = deque()

        for move, (dx, dy) in self.DIRS.items():
            nx = pos[0] + dx
            ny = pos[1] + dy
            if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                continue
            dist[ny][nx] = 1
            first_step[ny][nx] = move
            queue.append((nx, ny))

        while queue:
            x, y = queue.popleft()
            d = dist[y][x]
            move = first_step[y][x]
            reward = self._cell_reward(board[y][x])

            if reward:
                if battle_survival_mode:
                    threatened = False
                    for enemy_pos in enemy_positions.values():
                        if abs(enemy_pos[0] - x) + abs(enemy_pos[1] - y) <= 5:
                            threatened = True
                            break
                    if threatened:
                        reward = 0

            if reward:
                desirability = 80.0 if reward == 50 else 28.0
                desirability /= d
                if reward == 50:
                    desirability += 12.0
                if map_key.startswith("s_"):
                    desirability *= 1.85
                elif map_key == "treasure":
                    desirability *= 1.35
                enemy_d = enemy_dist[y][x]
                if enemy_d < d:
                    desirability *= 0.28
                elif enemy_d == d:
                    if player_id > enemy_owner[y][x]:
                        desirability *= 0.55
                    else:
                        desirability *= 0.9
                bias[move] += desirability

            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if dist[ny][nx] != -1:
                    continue
                if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    continue
                dist[ny][nx] = d + 1
                first_step[ny][nx] = move
                queue.append((nx, ny))

        return bias

    def _solo_near_heads(self, board, pos, rows, cols):
        nearby = set()
        for y in range(rows):
            for x in range(cols):
                if (x, y) == pos:
                    continue
                if not self._is_head(board[y][x]):
                    continue
                if abs(x - pos[0]) + abs(y - pos[1]) <= 2:
                    nearby.add((x, y))
        return nearby

    def _solo_collectible_targets(self, board, pos, blocked_positions, near_heads, rows, cols):
        dist = [[-1] * cols for _ in range(rows)]
        first_step = [[None] * cols for _ in range(rows)]
        queue = deque()
        blocked = set(blocked_positions)
        blocked.update(near_heads)

        total_ratio = {"N": 0.0, "S": 0.0, "W": 0.0, "E": 0.0}
        total_value = {"N": 0, "S": 0, "W": 0, "E": 0}
        target_count = {"N": 0, "S": 0, "W": 0, "E": 0}
        targets = []

        for move, (dx, dy) in self.DIRS.items():
            nx = pos[0] + dx
            ny = pos[1] + dy
            if not self._can_traverse(board, nx, ny, blocked, rows, cols):
                continue
            dist[ny][nx] = 1
            first_step[ny][nx] = move
            queue.append((nx, ny))

        while queue:
            x, y = queue.popleft()
            d = dist[y][x]
            move = first_step[y][x]
            reward = self._cell_reward(board[y][x])

            if reward:
                ratio = float(reward) / float(d)
                total_ratio[move] += ratio
                total_value[move] += reward
                target_count[move] += 1
                targets.append(
                    {
                        "move": move,
                        "distance": d,
                        "value": reward,
                        "ratio": ratio,
                        "pos": (x, y),
                    }
                )

            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if dist[ny][nx] != -1:
                    continue
                if not self._can_traverse(board, nx, ny, blocked, rows, cols):
                    continue
                dist[ny][nx] = d + 1
                first_step[ny][nx] = move
                queue.append((nx, ny))

        targets.sort(
            key=lambda item: (
                item["ratio"],
                total_ratio[item["move"]],
                total_value[item["move"]],
                item["value"],
                -item["distance"],
            ),
            reverse=True,
        )

        return {
            "targets": targets,
            "total_ratio": total_ratio,
            "total_value": total_value,
            "target_count": target_count,
        }

    def _solo_max_distance(self, board, start, blocked_positions, rows, cols):
        sx, sy = start
        if not self._can_traverse(board, sx, sy, blocked_positions - {start}, rows, cols):
            return -1

        dist = [[-1] * cols for _ in range(rows)]
        queue = deque([(sx, sy)])
        dist[sy][sx] = 0
        best = 0

        while queue:
            x, y = queue.popleft()
            d = dist[y][x]
            if d > best:
                best = d
            for dx, dy in self.DIRS.values():
                nx = x + dx
                ny = y + dy
                if not self._in_bounds(nx, ny, rows, cols):
                    continue
                if dist[ny][nx] != -1:
                    continue
                if not self._can_traverse(board, nx, ny, blocked_positions, rows, cols):
                    continue
                dist[ny][nx] = d + 1
                queue.append((nx, ny))

        return best

    def _apply_solo_greedy_scoring(
        self,
        board,
        pos,
        blocked_positions,
        normal_moves,
        map_name,
        rows,
        cols,
    ):
        near_heads = self._solo_near_heads(board, pos, rows, cols)
        target_data = self._solo_collectible_targets(
            board, pos, blocked_positions, near_heads, rows, cols
        )
        safe_count = len(normal_moves)

        if not target_data["targets"]:
            self.solo_target = None
            survival_blocked = set(blocked_positions)
            survival_blocked.update(near_heads)
            for move, entry in normal_moves.items():
                depth = self._solo_max_distance(
                    board, entry["target"], survival_blocked, rows, cols
                )
                score = entry["area"] * 220.0
                score += depth * 45.0
                score += (4 - entry["lookahead"]) * 35.0
                if move == self.direction:
                    score += 30.0
                for hx, hy in near_heads:
                    head_dist = abs(entry["target"][0] - hx) + abs(entry["target"][1] - hy)
                    if head_dist == 0:
                        score -= 15000.0
                    elif head_dist == 1:
                        score -= 3000.0
                entry["score"] = score
            return

        best_by_move = {}
        for move in normal_moves:
            best_by_move[move] = {
                "distance": self.INF,
                "reward": 0,
                "total_reward": 0,
            }

        for target in target_data["targets"]:
            move = target["move"]
            if move not in best_by_move:
                continue
            info = best_by_move[move]
            info["total_reward"] += target["value"]
            if (
                target["distance"] < info["distance"]
                or (
                    target["distance"] == info["distance"]
                    and target["value"] > info["reward"]
                )
            ):
                info["distance"] = target["distance"]
                info["reward"] = target["value"]

        for move, entry in normal_moves.items():
            area = entry["area"]
            lookahead = entry["lookahead"]
            guide = best_by_move.get(move)
            score = 0.0

            if safe_count == 1:
                score += 50000.0

            if area < 10:
                score -= (10 - area) * 5000.0
            else:
                score += area * 4.0

            if guide is None or guide["distance"] == self.INF:
                score += area * 12.0 + lookahead * 35.0
                if map_name.startswith("s_floodfill"):
                    score += area * 20.0
                elif map_name.startswith("s_choice"):
                    score += area * 8.0
                elif map_name.startswith("s_path"):
                    score += lookahead * 20.0
            else:
                distance = guide["distance"]
                reward = guide["reward"]
                total_reward = guide["total_reward"]

                if map_name.startswith("s_path"):
                    score += 30000.0
                    score -= distance * 2400.0
                    score += reward * 65.0
                    score += total_reward * 3.0
                    score += lookahead * 10.0
                elif map_name.startswith("s_choice"):
                    score += 22000.0
                    score += total_reward * 35.0
                    score -= distance * 900.0
                    score += reward * 25.0
                    score += area * 6.0
                else:
                    score += 24000.0
                    score += area * 18.0
                    score += total_reward * 12.0
                    score -= distance * 700.0
                    score += reward * 20.0
                    score += lookahead * 12.0

            if move == self.direction:
                score += 35.0

            for hx, hy in near_heads:
                head_dist = abs(entry["target"][0] - hx) + abs(entry["target"][1] - hy)
                if head_dist == 0:
                    score -= 15000.0
                elif head_dist == 1:
                    score -= 3000.0

            entry["score"] = score

        chosen_target = None
        if self.solo_target is not None:
            for target in target_data["targets"]:
                if target["pos"] != self.solo_target:
                    continue
                move = target["move"]
                entry = normal_moves.get(move)
                if entry is not None and entry["area"] > 10:
                    chosen_target = target
                break

        for target in target_data["targets"]:
            if chosen_target is not None:
                break
            move = target["move"]
            entry = normal_moves.get(move)
            if entry is None:
                continue
            if entry["area"] <= 10:
                continue
            chosen_target = target

        if chosen_target is None:
            self.solo_target = None
            return

        move = chosen_target["move"]
        self.solo_target = chosen_target["pos"]
        entry = normal_moves[move]
        move_ratio = target_data["total_ratio"][move]
        move_value = target_data["total_value"][move]
        move_count = target_data["target_count"][move]
        bonus = 0.0

        if map_name.startswith("s_path"):
            bonus += 22000.0
            bonus += chosen_target["ratio"] * 8000.0
            bonus += move_ratio * 2500.0
            bonus -= chosen_target["distance"] * 700.0
        elif map_name.startswith("s_floodfill"):
            bonus += 12000.0
            bonus += chosen_target["ratio"] * 4500.0
            bonus += move_value * 40.0
            bonus += move_count * 250.0
        elif map_name.startswith("s_choice"):
            bonus += 18000.0
            bonus += chosen_target["ratio"] * 6500.0
            bonus += move_value * 55.0
            bonus += move_count * 500.0
        else:
            bonus += 10000.0 + chosen_target["ratio"] * 4000.0

        bonus += 5000.0
        entry["score"] += bonus

    # -----------------------------
    # Scoring
    # -----------------------------
    def _mode_weights(self, solo_mode, duel_mode, battle_survival_mode):
        if solo_mode:
            return {
                "area": 5.2,
                "territory": 0.25,
                "component_reward": 0.8,
                "reward": 3.2,
                "lookahead": 18.0,
                "bias": 0.35,
                "near_reward": 12.0,
                "enemy": 12.0,
                "contest": 18.0,
                "dead_end": 320.0,
                "aggression": 4.0,
            }
        if battle_survival_mode:
            return {
                "area": 9.8,
                "territory": 3.4,
                "component_reward": 0.0,
                "reward": 0.35,
                "lookahead": 25.0,
                "bias": 0.0,
                "near_reward": 0.0,
                "enemy": 62.0,
                "contest": 135.0,
                "dead_end": 440.0,
                "aggression": 0.0,
            }
        if duel_mode:
            return {
                "area": 7.6,
                "territory": 2.4,
                "component_reward": 1.0,
                "reward": 4.3,
                "lookahead": 20.0,
                "bias": 1.0,
                "near_reward": 18.0,
                "enemy": 44.0,
                "contest": 120.0,
                "dead_end": 360.0,
                "aggression": 12.0,
            }
        return {
            "area": 7.1,
            "territory": 1.7,
            "component_reward": 1.2,
            "reward": 4.6,
            "lookahead": 19.0,
            "bias": 1.0,
            "near_reward": 22.0,
            "enemy": 36.0,
            "contest": 96.0,
            "dead_end": 330.0,
            "aggression": 8.0,
        }

    def _score_position(
        self,
        metrics,
        lookahead,
        immediate_reward,
        collectible_bias,
        enemy_d,
        contested_by,
        nearest_enemy,
        player_id,
        weights,
        map_name,
        move,
        enemy_space_reduction,
    ):
        map_key = self._normalize_map_name(map_name)
        score = 0.0
        score += metrics["area"] * weights["area"]
        score += metrics["territory"] * weights["territory"]
        score += metrics["reward_sum"] * weights["component_reward"]
        score += immediate_reward * weights["reward"]
        score += lookahead * weights["lookahead"]
        score += collectible_bias * weights["bias"]
        score += enemy_space_reduction * 0.4

        if metrics["nearest_reward"] != self.INF:
            score += weights["near_reward"] / (metrics["nearest_reward"] + 1.0)

        if enemy_d <= 1:
            score -= weights["enemy"] * 2.2
        elif enemy_d == 2:
            score -= weights["enemy"] * 0.95
        elif enemy_d == 3:
            score -= weights["enemy"] * 0.35

        if contested_by is not None:
            if contested_by < player_id:
                score -= weights["contest"] * 1.2
            else:
                score -= weights["contest"] * 0.65

        if lookahead == 0 and metrics["area"] <= 6:
            score -= weights["dead_end"]
        elif lookahead <= 1 and metrics["area"] <= 12:
            score -= weights["dead_end"] * 0.55

        if move == self.direction:
            score += 6.0

        if map_key.startswith("s_path"):
            score += collectible_bias * 0.65 + immediate_reward * 1.2
        elif map_key.startswith("s_floodfill"):
            score += metrics["area"] * 1.0 + lookahead * 6.0
        elif map_key.startswith("s_choice"):
            score += metrics["reward_sum"] * 0.8 + collectible_bias * 0.4
        elif map_key == "treasure":
            score += immediate_reward * 2.0 + metrics["reward_sum"] * 0.35

        if nearest_enemy <= 2 and metrics["territory"] > 0 and metrics["area"] >= 16:
            score += weights["aggression"] * (3 - nearest_enemy)
        elif nearest_enemy <= 1 and metrics["area"] <= 10:
            score -= weights["aggression"] * 3.0

        return score

    # -----------------------------
    # Special action evaluation
    # -----------------------------
    def _evaluate_boost(
        self,
        board,
        pos,
        player_id,
        occupied_now,
        occupied_heads,
        enemy_positions,
        enemy_dist,
        enemy_owner,
        weights,
        map_name,
        normal_moves,
        best_normal,
        rows,
        cols,
        duel_enemy_pos,
        baseline_enemy_area,
    ):
        if self.direction not in self.DIRS:
            return None

        dx, dy = self.DIRS[self.direction]
        mx = pos[0] + dx
        my = pos[1] + dy
        lx = pos[0] + 2 * dx
        ly = pos[1] + 2 * dy

        if not self._is_safe_normal_step(board, mx, my, self.direction, occupied_heads, rows, cols):
            return None
        if not self._is_safe_normal_step(board, lx, ly, self.direction, occupied_heads, rows, cols):
            return None

        metrics = self._analyze_candidate(
            board,
            (lx, ly),
            occupied_now,
            enemy_dist,
            enemy_owner,
            player_id,
            rows,
            cols,
        )
        lookahead = self._count_safe_neighbors(board, (lx, ly), occupied_now, rows, cols)
        landing_reward = self._cell_reward(board[ly][lx])
        mid_reward = self._cell_reward(board[my][mx])
        enemy_d = enemy_dist[ly][lx]
        nearest_enemy = self._nearest_enemy_distance((lx, ly), enemy_positions)
        enemy_space_reduction = 0.0
        if duel_enemy_pos is not None:
            blocked_after = set(occupied_now)
            blocked_after.add((lx, ly))
            enemy_area_after = self._reachable_area_from_source(
                board, duel_enemy_pos, blocked_after, rows, cols
            )
            enemy_space_reduction = max(0.0, baseline_enemy_area - enemy_area_after)

        score = self._score_position(
            metrics,
            lookahead,
            landing_reward,
            0.0,
            enemy_d,
            None,
            nearest_enemy,
            player_id,
            weights,
            map_name,
            self.direction,
            enemy_space_reduction,
        )
        score += 18.0
        score += mid_reward * 0.5

        force = False
        if landing_reward:
            score += 135.0
            force = True
        if best_normal is not None and self.direction in normal_moves:
            forward_normal = normal_moves[self.direction]
            if metrics["area"] >= forward_normal["area"] + 10:
                score += 55.0
            if enemy_d > forward_normal["enemy_d"] + 1:
                score += 24.0
        elif best_normal is None:
            force = True

        if map_name.startswith("s_"):
            score += 24.0
            if metrics["area"] >= 18:
                score += 20.0
        if lookahead == 0 and metrics["area"] <= 8:
            score -= 180.0

        return {
            "move": "+",
            "score": score,
            "lookahead": lookahead,
            "immediate_reward": landing_reward,
            "enemy_d": enemy_d,
            "nearest_enemy": nearest_enemy,
            "enemy_space_reduction": enemy_space_reduction,
            "force": force,
            "target": (lx, ly),
            "area": metrics["area"],
            "territory": metrics["territory"],
            "reward_sum": metrics["reward_sum"],
        }

    def _evaluate_phase(
        self,
        board,
        pos,
        player_id,
        occupied_now,
        occupied_heads,
        enemy_positions,
        enemy_dist,
        enemy_owner,
        weights,
        map_name,
        normal_moves,
        best_normal,
        rows,
        cols,
        duel_enemy_pos,
        baseline_enemy_area,
    ):
        if self.phase_uses <= 0:
            return None
        if self.direction not in self.DIRS:
            return None

        dx, dy = self.DIRS[self.direction]
        mx = pos[0] + dx
        my = pos[1] + dy
        lx = pos[0] + 2 * dx
        ly = pos[1] + 2 * dy

        if not self._in_bounds(lx, ly, rows, cols):
            return None
        if self.last_pos is not None and (lx, ly) == self.last_pos:
            return None

        landing_cell = board[ly][lx]
        if self._cell_text(landing_cell) == "#":
            return None
        if self._is_trail(landing_cell):
            return None
        timed = self._timed_value(landing_cell)
        if timed is not None and timed > 0:
            return None
        if not self._cell_is_walkable(landing_cell):
            return None

        metrics = self._analyze_candidate(
            board,
            (lx, ly),
            occupied_now,
            enemy_dist,
            enemy_owner,
            player_id,
            rows,
            cols,
        )
        lookahead = self._count_safe_neighbors(board, (lx, ly), occupied_now, rows, cols)
        landing_reward = self._cell_reward(landing_cell)
        enemy_d = enemy_dist[ly][lx]
        nearest_enemy = self._nearest_enemy_distance((lx, ly), enemy_positions)
        through_blocked = not self._can_traverse(board, mx, my, {pos}, rows, cols)
        enemy_space_reduction = 0.0
        if duel_enemy_pos is not None:
            blocked_after = set(occupied_now)
            blocked_after.add((lx, ly))
            enemy_area_after = self._reachable_area_from_source(
                board, duel_enemy_pos, blocked_after, rows, cols
            )
            enemy_space_reduction = max(0.0, baseline_enemy_area - enemy_area_after)

        score = self._score_position(
            metrics,
            lookahead,
            landing_reward,
            0.0,
            enemy_d,
            None,
            nearest_enemy,
            player_id,
            weights,
            map_name,
            self.direction,
            enemy_space_reduction,
        )
        score -= 35.0

        force = False
        if through_blocked:
            score += 120.0
            force = True
        if landing_reward:
            score += 145.0
        if best_normal is None:
            force = True
            score += 80.0
        elif len(normal_moves) <= 1 and metrics["area"] > best_normal["area"] + 10:
            score += 90.0
        elif metrics["area"] > best_normal["area"] + 18:
            score += 55.0

        if lookahead == 0 and metrics["area"] <= 8:
            score -= 200.0
            force = False

        return {
            "move": "P",
            "score": score,
            "lookahead": lookahead,
            "immediate_reward": landing_reward,
            "enemy_d": enemy_d,
            "nearest_enemy": nearest_enemy,
            "enemy_space_reduction": enemy_space_reduction,
            "force": force,
            "target": (lx, ly),
            "area": metrics["area"],
            "territory": metrics["territory"],
            "reward_sum": metrics["reward_sum"],
            "through_blocked": through_blocked,
        }

    def _should_emp(
        self,
        move,
        chosen_entry,
        pos,
        player_id,
        enemy_positions,
        solo_mode,
        duel_mode,
        info,
    ):
        if solo_mode:
            return False
        if not enemy_positions:
            return False
        if self.turn_count < 4:
            return False
        if self.emp_uses >= (2 if duel_mode else 3):
            return False
        if self.turn_count - self.last_emp_turn < 9:
            return False
        if move == "P":
            return False
        if chosen_entry is None:
            return False
        if chosen_entry.get("area", 0) < 14:
            return False

        if not self._emp_ready_from_info(info, player_id):
            return False

        close_enemies = 0
        for enemy_pos in enemy_positions.values():
            if abs(enemy_pos[0] - pos[0]) + abs(enemy_pos[1] - pos[1]) <= 2:
                close_enemies += 1

        if close_enemies == 0:
            return False
        if duel_mode:
            return True
        return close_enemies >= 2 or chosen_entry.get("territory", 0) > 8

    def _emp_ready_from_info(self, info, player_id):
        cooldowns = info.get("cooldowns")
        if cooldowns is None:
            return True

        def extract_numeric(value):
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
            return None

        if isinstance(cooldowns, dict):
            own = None
            for key in (player_id, str(player_id), "p" + str(player_id), "player" + str(player_id)):
                if key in cooldowns:
                    own = cooldowns[key]
                    break
            if own is None:
                own = cooldowns
            if isinstance(own, dict):
                for key in ("emp", "EMP", "x", "X"):
                    if key in own:
                        value = extract_numeric(own[key])
                        if value is not None:
                            return value <= 0
                return True
            value = extract_numeric(own)
            return value is None or value <= 0

        if isinstance(cooldowns, (list, tuple)):
            index = player_id - 1
            if 0 <= index < len(cooldowns):
                value = cooldowns[index]
                if isinstance(value, dict):
                    for key in ("emp", "EMP", "x", "X"):
                        if key in value:
                            numeric = extract_numeric(value[key])
                            if numeric is not None:
                                return numeric <= 0
                    return True
                numeric = extract_numeric(value)
                return numeric is None or numeric <= 0

        return True

    # -----------------------------
    # Fallbacks
    # -----------------------------
    def _best_desperate_direction(self, board, pos, occupied_heads, rows, cols):
        current = self.direction
        if current in self.DIRS:
            dx, dy = self.DIRS[current]
            nx = pos[0] + dx
            ny = pos[1] + dy
            if self._in_bounds(nx, ny, rows, cols):
                return current

        for move in ("N", "S", "W", "E"):
            dx, dy = self.DIRS[move]
            nx = pos[0] + dx
            ny = pos[1] + dy
            if self._in_bounds(nx, ny, rows, cols):
                return move
        return "N"

    def _emergency_fallback(self, board, pos):
        rows = len(board) if board else 0
        cols = len(board[0]) if rows and board[0] else 0
        if rows == 0 or cols == 0:
            return self.direction or "N"

        preferred = []
        if self.direction in self.DIRS:
            preferred.append(self.direction)
        preferred.extend(["N", "S", "W", "E"])

        seen = set()
        for move in preferred:
            if move in seen:
                continue
            seen.add(move)
            dx, dy = self.DIRS.get(move, (0, 0))
            nx = pos[0] + dx
            ny = pos[1] + dy
            if self._in_bounds(nx, ny, rows, cols):
                cell = board[ny][nx]
                if self._cell_text(cell) != "#" and not self._is_trail(cell):
                    timed = self._timed_value(cell)
                    if timed is None or timed == 0:
                        return move
        return preferred[0] if preferred else "N"

    # -----------------------------
    # Score parsing
    # -----------------------------
    def _score_total(self, scores):
        if scores is None:
            return 0
        if isinstance(scores, dict):
            total = 0
            for value in scores.values():
                if isinstance(value, int):
                    total += value
            return total
        if isinstance(scores, (list, tuple)):
            total = 0
            for value in scores:
                if isinstance(value, int):
                    total += value
            return total
        return 0
