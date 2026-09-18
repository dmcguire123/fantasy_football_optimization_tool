"""Turning a raw league payload into League objects."""

from conftest import WEEK, league_payload

from ffopt.league import parse_league, parse_settings, parse_team_name


def test_settings_are_read_from_the_payload(league):
    settings = league.settings

    assert settings.name == "Test Gridiron League"
    assert settings.size == 3
    assert settings.current_week == WEEK
    assert settings.uses_faab
    assert settings.faab_budget == 100


def test_ppr_is_inferred_from_the_reception_scoring_item():
    settings = parse_settings(league_payload())
    assert settings.scoring_type == "PPR"


def test_starting_slots_expand_to_one_entry_per_slot(starting_slots):
    # One QB, two RB, two WR, one TE, one FLEX, one D/ST, one K.
    assert starting_slots.count(0) == 1
    assert starting_slots.count(2) == 2
    assert starting_slots.count(4) == 2
    assert starting_slots.count(23) == 1
    assert 20 not in starting_slots
    assert 21 not in starting_slots
    assert len(starting_slots) == 9


def test_team_names_handle_both_espn_shapes():
    assert parse_team_name({"name": "My Squad"}) == "My Squad"
    assert parse_team_name({"location": "Deep", "nickname": "Backfield"}) == "Deep Backfield"
    assert parse_team_name({"id": 9}) == "Team 9"


def test_owner_names_resolve_through_the_member_list(league):
    team = league.team_by_id(1)
    assert team.owner_names == ["Dana Reed"]


def test_faab_remaining_subtracts_what_was_spent(league):
    assert league.team_by_id(1).faab_remaining == 75.0
    assert league.team_by_id(2).faab_remaining == 90.0
    assert league.team_by_id(3).faab_remaining == 40.0


def test_rosters_are_attached_with_slots(league):
    team = league.team_by_id(1)

    assert len(team.roster) == 14
    assert len(team.starters) == 9
    assert team.player_by_id(102).lineup_slot_name == "BE"
    assert team.player_by_id(101).lineup_slot_name == "QB"


def test_bye_weeks_come_from_the_pro_team_schedule(league):
    bye_back = league.team_by_id(1).player_by_id(104)

    assert bye_back.pro_team == "GB"
    assert bye_back.on_bye
    assert bye_back.effective_projection == 0.0


def test_schedule_resolves_this_week_opponent(league):
    opponent = league.opponent_for(1)

    assert opponent is not None
    assert opponent.team_id == 2
    assert league.opponent_for(3) is None


def test_service_finds_my_team_from_settings(service):
    assert service.my_team().team_id == 1


def test_parse_league_accepts_an_explicit_week():
    parsed = parse_league(league_payload(), week=3, season=2025)
    assert parsed.week == 3
