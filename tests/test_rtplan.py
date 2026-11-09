from pathlib import Path
import numpy as np
import pytest

from tps.beamdata.csv_loader import load_curves, load_table
from tps.checks.plan_checks import check_plan
from tps.dicom_io.rtplan import read_rtplan
from tps.machine.model import load_machine

PRECISE = Path(__file__).parent / "dados" / "elekta_precise.yaml"
from tests.synthetic import make_plan


@pytest.fixture
def write(tmp_path):
    def _w(ds, name="RP.dcm"):
        p = tmp_path / name
        ds.save_as(p, enforce_file_format=True)
        return p
    return _w


def test_vmat_precise_parse(write):
    plan = read_rtplan(write(make_plan("precise", "VMAT", mu=300, n_cp=5)))
    b = plan.beams[0]
    assert b.technique == "VMAT"
    assert b.n_leaf_pairs == 40
    assert len(b.control_points) == 5
    assert sum(cp.mu_segment for cp in b.control_points) == pytest.approx(300)
    assert b.control_points[3].energy_mv == 6       # herdado do CP0
    assert b.control_points[0].jaw_x is not None


def test_agility_has_no_x_jaw(write):
    plan = read_rtplan(write(make_plan("agility", "VMAT")))
    b = plan.beams[0]
    assert b.n_leaf_pairs == 80
    assert all(cp.jaw_x is None for cp in b.control_points)


def test_3d_inherits_aperture(write):
    plan = read_rtplan(write(make_plan("precise", "3D", mu=150)))
    b = plan.beams[0]
    assert b.technique == "3D"
    np.testing.assert_allclose(b.control_points[1].mlc_b, b.control_points[0].mlc_b)


def test_checks_detect_wrong_machine(write):
    plan = read_rtplan(write(make_plan("agility", "VMAT")))
    findings = check_plan(plan, load_machine(PRECISE))
    assert any(f.code == "MLC-PAIRS" for f in findings)


def test_checks_clean_plan(write):
    plan = read_rtplan(write(make_plan("precise", "VMAT")))
    findings = check_plan(plan, load_machine(PRECISE))
    assert not [f for f in findings if f.level == "ERRO"]


def test_energy_not_commissioned(write):
    plan = read_rtplan(write(make_plan("precise", "3D", energy=18)))
    findings = check_plan(plan, load_machine(PRECISE))
    assert any(f.code == "ENERGY" for f in findings)


def test_beamdata_templates():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "templates" / "beamdata"
    pdd = load_curves(root / "pdd.csv", "pdd")
    assert len(pdd) == 1
    assert load_table(root / "fatores_output.csv", "fatores_output")[1]["fator_output"] == 0.93
