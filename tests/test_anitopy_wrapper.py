from trackma.messenger import Messenger
from trackma.parser.anitopy import AnitopyWrapper


def _parse(filename):
    return AnitopyWrapper(Messenger(None, 'Test'), filename)


def test_anitopy_wrapper_ignores_non_episode_pv_title():
    parser = _parse('Arknights 2024 Special Commemorative Animation PV')

    assert parser.getName() is None
    assert parser.getEpisode() == 1


# Note that this is misbehavior of anitopy but we still don't want to crash.
def test_anitopy_wrapper_recovers_episode_from_title_when_decimal_is_misparsed():
    parser = _parse('2.5 Jigen no Ririsa E01 [1080p][E-AC-3][JapDub][GerSub][Web-DL].mkv')

    assert parser.getName() == 'Jigen no Ririsa E01'
    assert parser.getEpisode() == 1


# Same here
def test_anitopy_wrapper_handles_dotted_episode_number_in_filename():
    parser = _parse('Mr. Robot - Season 02 - 05 - eps2.3_logic-b0mb.hc Bluray-1080p.mkv')

    assert parser.getName() == 'Mr. Robot Season 02'
    assert parser.getEpisode() == 1


def test_anitopy_wrapper_ignores_url_like_filenames():
    parser = _parse('watch?v=dQw4w9WgXcQ')

    assert parser.getName() is None
    assert parser.getEpisode() == 1


def test_anitopy_wrapper_keeps_valid_filenames_with_special_characters():
    parser = _parse('My Show ? Special & Stuff = Fun E03.mkv')

    assert parser.getName() == 'My Show ? Special & Stuff = Fun'
    assert parser.getEpisode() == 3


def test_anitopy_wrapper_parses_episode_ranges():
    parser = _parse('NEET Kunoichi to Naze ka Dousei Hajimemashita E01-E02 [1080p][AAC][JapDub][GerSub][Web-DL].mkv')

    assert parser.getEpisode() == 2
    assert parser.getEpisodeNumbers() == (1, 2)


def test_anitopy_wrapper_ignores_hash_like_filenames():
    parser = _parse('e8cf4075112ea1a0.mp4')

    assert parser.getName() is None
    assert parser.getEpisode() == 1


def test_anitopy_wrapper_allows_simple_suffix_after_episode_number():
    parser = _parse('Ore wa Seikan Kokka no Akutoku Ryoushu! E1P [1080p][AAC][JapDub][GerEngSub][Web-DL].mkv')

    assert parser.getName() == 'Ore wa Seikan Kokka no Akutoku Ryoushu!'
    assert parser.getEpisode() == 1


def test_anitopy_wrapper_allows_episode_numbers_before_colon():
    parser = _parse('Sasameki Koto E02: Cute People')

    assert parser.getName() == 'Sasameki Koto'
    assert parser.getEpisode() == 2


def test_anitopy_wrapper_survives_anitopy_internal_crashes():
    parser = _parse('Beach episode | The amazing digital circus S1:E7')

    assert parser.getName() is None or isinstance(parser.getName(), str)
    assert parser.getEpisode() == 7
