"""Tests for ProcessingFilter, in particular the per-crystal deduplication.

A pin that was mounted and collected twice produces two dataset folders that the
dataset tag keeps apart but that measure the same crystal.  Only the better of
them should reach refinement, and the loser should be absent from the derived
list rather than marked in it -- the list is regenerated from scratch, so
absence IS the decision.  Why it was set aside belongs in info.json, which is
what ``superseded`` carries out of the filter.
"""

import pytest

from avicenna.utils.utils import ProcessingFilter, ProcessingScraper


def result(tag, dataset_code, rpim=0.04, cc12=0.99, cc12_outer=0.50,
           resolution=1.80, completeness=99.0, multiplicity=5.0):
    """One scraped Aimless result, in the shape ProcessingScraper produces.

    Every multi-valued statistic is [overall, inner shell, outer shell].
    """
    return {
        "dataset_code": dataset_code,
        "proc_path": f"/proc/{tag}",
        "result_path": f"/proc/{tag}/native",
        "pipeline_subdir": f"/proc/{tag}/native/index_1_100",
        "outcome": "completed",
        "Rpim (within I+/I-)": [str(rpim), "0.010", "0.400"],
        "Mn(I) half-set correlation CC(1/2)": [str(cc12), "0.999", str(cc12_outer)],
        "High resolution limit": ["20.00", "4.00", str(resolution)],
        "Completeness": [str(completeness), "100.0", "95.0"],
        "Multiplicity": [str(multiplicity), "6.0", "4.0"],
    }


# --------------------------------------------------------------------------- #
# The quality gate is unchanged                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kwargs", [
    {"rpim": 0.90},              # above RPIM_MAX
    {"completeness": 50.0},      # below COMPLETENESS_MIN
    {"multiplicity": 1.0},       # below MULTIPLICITY_MIN
    {"cc12": 0.10},              # below CC12_OVERALL_MIN
    {"cc12_outer": 0.05},        # below CC12_OUTER_MIN
])
def test_poor_data_is_still_dropped(kwargs):
    assert ProcessingFilter().run([result("a", "FRS005_10", **kwargs)]) == []


def test_unprocessed_results_never_reach_the_filter():
    bad = result("a", "FRS005_10")
    bad["outcome"] = "failed"
    assert ProcessingFilter().run([bad]) == []


def test_missing_statistics_do_not_crash_the_filter():
    broken = result("a", "FRS005_10")
    del broken["Rpim (within I+/I-)"]
    assert ProcessingFilter().run([broken]) == []


# --------------------------------------------------------------------------- #
# Per-crystal deduplication                                                    #
# --------------------------------------------------------------------------- #

def test_two_collections_of_one_pin_keep_only_the_better():
    first = result("als_20251206_FRS005_10_1", "FRS005_10", rpim=0.041)
    second = result("als_20251209_FRS005_10_1", "FRS005_10", rpim=0.052)

    filt = ProcessingFilter()
    kept = filt.run([first, second])

    assert kept == [first]
    assert [r for r, _ in filt.superseded] == [second]
    assert filt.superseded[0][1] == "als_20251206_FRS005_10_1"


def test_the_loser_is_absent_from_the_list_not_marked_in_it():
    first = result("als_20251206_FRS005_10_1", "FRS005_10", rpim=0.041)
    second = result("als_20251209_FRS005_10_1", "FRS005_10", rpim=0.052)
    kept = ProcessingFilter().run([first, second])
    assert all(r is not second for r in kept)


def test_a_spelling_difference_still_counts_as_one_crystal():
    """Folder names are typed by hand; FRS006_4 and FRS006_04 are one pin."""
    padded = result("als_20251206_FRS006_04_1", "FRS006_04", rpim=0.060)
    bare = result("als_20251209_FRS006_4_1", "FRS006_4", rpim=0.045)
    assert ProcessingFilter().run([padded, bare]) == [bare]


def test_ties_fall_through_to_cc_half_then_resolution():
    worse_cc = result("a", "FRS005_10", rpim=0.040, cc12=0.970)
    better_cc = result("b", "FRS005_10", rpim=0.040, cc12=0.995)
    assert ProcessingFilter().run([worse_cc, better_cc]) == [better_cc]

    coarse = result("c", "FRS005_10", rpim=0.040, cc12=0.990, resolution=2.10)
    fine = result("d", "FRS005_10", rpim=0.040, cc12=0.990, resolution=1.80)
    assert ProcessingFilter().run([coarse, fine]) == [fine]


def test_three_collections_leave_one_winner_and_two_recorded_losers():
    best = result("a", "FRS005_10", rpim=0.030)
    middle = result("b", "FRS005_10", rpim=0.045)
    worst = result("c", "FRS005_10", rpim=0.070)

    filt = ProcessingFilter()
    kept = filt.run([middle, worst, best])

    assert kept == [best]
    assert {id(r) for r, _ in filt.superseded} == {id(middle), id(worst)}


def test_different_pins_are_left_alone():
    one = result("a", "FRS005_12")
    two = result("b", "FRS005_13")
    filt = ProcessingFilter()
    assert len(filt.run([one, two])) == 2
    assert filt.superseded == []


def test_a_code_that_cannot_be_parsed_is_not_grouped_on_a_guess():
    one = result("a", "AAA")
    two = result("b", "BBB")
    assert len(ProcessingFilter().run([one, two])) == 2


def test_input_order_is_preserved():
    one = result("a", "FRS005_12")
    two = result("b", "FRS005_13")
    assert ProcessingFilter().run([two, one]) == [two, one]


def test_superseded_is_empty_before_a_run_and_reset_between_runs():
    filt = ProcessingFilter()
    assert filt.superseded == []
    filt.run([result("a", "FRS005_10", rpim=0.04),
              result("b", "FRS005_10", rpim=0.05)])
    assert len(filt.superseded) == 1
    filt.run([result("c", "FRS005_12")])
    assert filt.superseded == []


def test_the_quality_gate_runs_before_deduplication():
    """A collection that fails the gate must not win its crystal by default."""
    good = result("a", "FRS005_10", rpim=0.060)
    unusable = result("b", "FRS005_10", rpim=0.005, completeness=10.0)
    filt = ProcessingFilter()
    assert filt.run([good, unusable]) == [good]
    assert filt.superseded == []


# --------------------------------------------------------------------------- #
# Inputs the filter must survive                                               #
# --------------------------------------------------------------------------- #

def test_results_that_name_no_folder_are_not_merged_together():
    """Without a folder there is nothing to deduplicate on, so each such
    result stands alone rather than all of them competing as one."""
    first = result("a", "FRS005_10")
    second = result("b", "FRS005_11")
    for r in (first, second):
        del r["proc_path"]
    assert ProcessingFilter().run([first, second]) == [first, second]


AIMLESS_TAIL = """\
                                       Overall  InnerShell  OuterShell
High resolution limit                     1.85      8.27      1.85
Rpim (within I+/I-)                      0.028     0.014     0.401
Mn(I) half-set correlation CC(1/2)       0.998     0.999    -0.021
Completeness                              99.8     100.0      98.2
Multiplicity                              12.4      13.1       9.6
Average unit cell:  78.90  78.90  37.10  90.00  90.00  90.00
Average mosaicity:       0.12
Space group: P 43 21 2
"""


def test_a_negative_outer_shell_correlation_is_scraped_as_written(tmp_path):
    """Dropping the minus sign's token shifts the columns, and the outer
    shell would then be read from the inner shell."""
    log = tmp_path / "3_aimless.log"
    log.write_text(AIMLESS_TAIL)
    scraped = ProcessingScraper(str(tmp_path)).scrape_one_log(
        {"aimless_log": str(log)})
    assert scraped["Mn(I) half-set correlation CC(1/2)"] == ["0.998", "0.999", "-0.021"]


def test_a_negative_outer_shell_correlation_fails_the_gate():
    assert ProcessingFilter().run([result("a", "FRS005_10", cc12_outer=-0.021)]) == []
