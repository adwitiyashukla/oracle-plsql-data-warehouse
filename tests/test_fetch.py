import io

import pytest

import granary

HEADER = ",".join(granary.HEADER).encode()


def row(npi, state, city="Columbus", last="Smith", generic="Lisinopril"):
    fields = [npi, last, "Ann", city, state, "39", "Family Practice", "Claim-Specialty", "Lisinopril",
              generic, "20", "20", "600", "150.00", "15", "", "15", "15", "110.00", "450", "", "11"]
    return ",".join(fields).encode()


def run(lines, states=("OH",)):
    out = io.StringIO()
    counts = granary.filter_rows(lines, list(states), out)
    return counts, out.getvalue().split("\n")


def test_keeps_only_the_wanted_state():
    counts, lines = run([HEADER, row("1", "OH"), row("2", "KY"), row("3", "OH")])
    assert counts == (3, 2, 0)
    assert [line.split(",")[0] for line in lines[1:-1]] == ["1", "3"]


def test_several_states():
    counts, _ = run([HEADER, row("1", "OH"), row("2", "KY"), row("3", "IN")], states=("OH", "IN"))
    assert counts == (3, 2, 0)


def test_state_inside_a_quoted_city_is_not_a_match():
    counts, _ = run([HEADER, row("1", "KY", city='"Ada,OH,"')])
    assert counts == (1, 0, 0)


def test_quoted_drug_name_with_commas_is_kept_as_is():
    line = row("1", "OH", generic='"Diphth,pertuss(Acell),tet Vac"')
    counts, lines = run([HEADER, line])
    assert counts == (1, 1, 0)
    assert lines[1] == line.decode()


def test_header_is_written_first_and_lines_end_with_lf():
    counts, lines = run([HEADER + b"\r\n", row("1", "OH") + b"\r\n"])
    assert lines[0] == HEADER.decode()
    assert lines[-1] == ""
    assert all("\r" not in line for line in lines)


def test_wrong_header_stops_the_download():
    with pytest.raises(ValueError, match="unexpected header"):
        run([b"NPI,State", row("1", "OH")])


def test_row_with_missing_columns_is_counted_as_malformed():
    counts, _ = run([HEADER, b"1,Smith,Ann,Columbus,OH,39"])
    assert counts == (1, 0, 1)


def test_windows_1252_names_are_decoded():
    encoded = ("Pe" + chr(241) + "a").encode("cp1252")
    line = row("1", "OH", last="NAME").replace(b"NAME", encoded)
    counts, lines = run([HEADER, line])
    assert counts == (1, 1, 0)
    assert lines[1].split(",")[1] == "Pe" + chr(241) + "a"


def write_config(tmp_path, years="[2022, 2023]", states='["OH"]', repeats="3"):
    path = tmp_path / "config.toml"
    path.write_text(
        f"[data]\nyears = {years}\nstates = {states}\n"
        '[data.urls]\n2022 = "https://example.com/a.csv"\n2023 = "https://example.com/b.csv"\n'
        '[db]\ndsn = "localhost:1521/FREEPDB1"\nuser = "granary"\npassword_env = "GRANARY_PASSWORD"\n'
        f"[bench]\nrepeats = {repeats}\n"
    )
    return path


def test_config_loads(tmp_path):
    cfg = granary.load_config(write_config(tmp_path))
    assert cfg["years"] == [2022, 2023]
    assert cfg["urls"][2023] == "https://example.com/b.csv"


@pytest.mark.parametrize(
    "change",
    [
        {"years": "[2023, 2022]"},
        {"years": "[2022, 2022]"},
        {"years": "[2030]"},
        {"states": '["Ohio"]'},
        {"states": '["oh"]'},
        {"repeats": "0"},
    ],
)
def test_bad_config_is_refused(tmp_path, change):
    with pytest.raises(ValueError):
        granary.load_config(write_config(tmp_path, **change))
