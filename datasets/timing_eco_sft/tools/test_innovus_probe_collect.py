from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


COLLECTOR = (
    Path(__file__).resolve().parents[1]
    / "pilot_10"
    / "templates"
    / "innovus_probe_collect.tcl"
)


class InnovusProbeCollectorTclTests(unittest.TestCase):
    def test_command_existence_boolean_is_normalized_for_typed_tsv(self) -> None:
        source = COLLECTOR.read_text(encoding="utf-8")
        self.assertIn(
            'set exists [expr {[llength [info commands $command]] > 0 ? "TRUE" : "FALSE"}]',
            source,
        )

    def test_constant_filter_uses_supported_enum_and_is_fail_closed(self) -> None:
        source = COLLECTOR.read_text(encoding="utf-8")
        self.assertIn(
            "proc ::sft_probe::not_constant_or_reject", source
        )
        self.assertIn("property_result $net {constant}", source)
        self.assertIn(
            "$classification in {no_constant none false 0}", source
        )
        self.assertIn("constant_net_filter_unsupported", source)
        self.assertNotIn("{is_constant constant}", source)

    def test_mark_unsupported_uses_balanced_single_line_rendering(self) -> None:
        source = COLLECTOR.read_text(encoding="utf-8")
        body = source[
            source.index("proc ::sft_probe::mark_unsupported") :
            source.index("proc ::sft_probe::items")
        ]
        self.assertIn(
            "regsub -all {[[:space:]]+} [string trim $detail] {_}", body
        )
        self.assertIn(
            "[format {SFT_PROBE_UNSUPPORTED capability=%s detail=%s}", body
        )
        self.assertNotIn('string map [list \\" \\"', body)

    def test_mark_unsupported_handles_whitespace_and_preserves_evidence(self) -> None:
        tclsh = shutil.which("tclsh")
        if not tclsh:
            self.skipTest("tclsh is unavailable")

        source = COLLECTOR.read_text(encoding="utf-8")
        main_marker = "\nif {[catch {::sft_probe::main}"
        self.assertIn(main_marker, source)
        definitions = source[: source.index(main_marker)]
        harness = definitions + r'''
set capability "help:test command"
set detail "first line\tsecond line\nthird line\rfourth line"
::sft_probe::mark_unsupported $capability $detail
::sft_probe::mark_unsupported $capability $detail
if {[llength $::sft_probe::unsupported] != 1} {
    error "duplicate unsupported evidence was not deduplicated"
}
set stored [lindex $::sft_probe::unsupported 0]
if {[lindex $stored 0] ne $capability || [lindex $stored 1] ne $detail} {
    error "unsupported evidence was altered"
}
puts SFT_MARK_UNSUPPORTED_UNIT_TEST_PASSED
'''

        with tempfile.TemporaryDirectory() as temporary_directory:
            script = Path(temporary_directory) / "mark_unsupported_test.tcl"
            script.write_text(harness, encoding="utf-8")
            result = subprocess.run(
                [tclsh, str(script)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual(
            2,
            result.stdout.count(
                "SFT_PROBE_UNSUPPORTED capability=help:test_command "
                "detail=first_line_second_line_third_line_fourth_line"
            ),
            result.stdout,
        )
        self.assertIn("SFT_MARK_UNSUPPORTED_UNIT_TEST_PASSED", result.stdout)

    def test_constant_enum_filter_accepts_only_explicit_false_values(self) -> None:
        tclsh = shutil.which("tclsh")
        if not tclsh:
            self.skipTest("tclsh is unavailable")

        source = COLLECTOR.read_text(encoding="utf-8")
        main_marker = "\nif {[catch {::sft_probe::main}"
        self.assertIn(main_marker, source)
        definitions = source[: source.index(main_marker)]
        harness = definitions + r'''
proc assert_equal {actual expected label} {
    if {$actual ne $expected} {
        error "$label: expected=<$expected> actual=<$actual>"
    }
}
proc get_property {object property} { return "" }
proc get_db {object property} {
    if {![info exists ::mock_constant]} {
        error "constant property unsupported"
    }
    return $::mock_constant
}

foreach false_value {no_constant none false 0 NO_CONSTANT} {
    set ::mock_constant $false_value
    set result [::sft_probe::not_constant_or_reject \
        late 1 -0.100 launch/Q capture/D data_net]
    assert_equal [dict get $result eligible] 1 \
        "$false_value must be accepted as non-constant"
}

set ::mock_constant constant_1
set result [::sft_probe::not_constant_or_reject \
    late 2 -0.100 launch/Q capture/D tie_net]
assert_equal [dict get $result eligible] 0 "constant enum must be rejected"
assert_equal [dict get $result reason] constant_net \
    "constant enum rejection reason"

unset ::mock_constant
set result [::sft_probe::not_constant_or_reject \
    late 3 -0.100 launch/Q capture/D unknown_net]
assert_equal [dict get $result eligible] 0 \
    "unsupported enum must fail closed"
assert_equal [dict get $result reason] constant_net_filter_unsupported \
    "unsupported enum rejection reason"

set ::mock_constant ""
set result [::sft_probe::not_constant_or_reject \
    late 4 -0.100 launch/Q capture/D empty_net]
assert_equal [dict get $result eligible] 0 "empty enum must fail closed"
assert_equal [dict get $result reason] constant_net_filter_unsupported \
    "empty enum rejection reason"
puts SFT_CONSTANT_ENUM_FILTER_UNIT_TEST_PASSED
'''

        with tempfile.TemporaryDirectory() as temporary_directory:
            script = Path(temporary_directory) / "constant_enum_filter_test.tcl"
            script.write_text(harness, encoding="utf-8")
            result = subprocess.run(
                [tclsh, str(script)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn(
            "SFT_CONSTANT_ENUM_FILTER_UNIT_TEST_PASSED", result.stdout
        )


if __name__ == "__main__":
    unittest.main()
