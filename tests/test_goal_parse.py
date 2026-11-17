import pytest

from bot.goal_parse import format_goal, parse_goal


@pytest.mark.parametrize("text,expected", [
    ("Yoga, 3 days", ("Yoga", 3)),
    ("Yoga 3", ("Yoga", 3)),
    ("yoga - 3x", ("yoga", 3)),
    ("Yoga 3 times a week", ("Yoga", 3)),
    ("Yoga, 3 days per week", ("Yoga", 3)),
    ("Yoga (3/wk)", ("Yoga", 3)),
    ("Yoga three days", ("Yoga", 3)),
    ("Affirmations, every day", ("Affirmations", 7)),
    ("Affirmations everyday", ("Affirmations", 7)),
    ("affirmations daily", ("affirmations", 7)),
    ("7 hours of sleep, 7 days", ("7 hours of sleep", 7)),
    ("7 hours of sleep 7", ("7 hours of sleep", 7)),
    ("7 hours of sleep, every day", ("7 hours of sleep", 7)),
    ("Walk 10,000 steps, 5 days", ("Walk 10,000 steps", 5)),
    ("I will meditate on 5 days per week", ("meditate", 5)),
    ("Alcohol-free, 6 days", ("Alcohol-free", 6)),
    ("Read once a week", ("Read", 1)),
    ("Lift weights 2x/week", ("Lift weights", 2)),
    ("Stretch: 4", ("Stretch", 4)),
    ("Drink 100 oz water, 5 days.", ("Drink 100 oz water", 5)),
])
def test_parses_common_ways_of_writing_a_goal(text, expected):
    assert parse_goal(text) == expected


@pytest.mark.parametrize("text", [
    "Yoga",                 # no days
    "Sleep 7 hours",        # the number is part of the goal, not days
    "Walk 10000 steps",     # 10000 isn't 1-7
    "Yoga 8 days",          # more than 7
    "3 days",               # no goal
    "",
])
def test_asks_for_a_fix_when_it_cant_tell(text):
    assert isinstance(parse_goal(text), str)


def test_round_trip_for_editing():
    for name, days in [("Yoga", 3), ("Affirmations", 7), ("Read", 1), ("7 hours of sleep", 7)]:
        assert parse_goal(format_goal(name, days)) == (name, days)
