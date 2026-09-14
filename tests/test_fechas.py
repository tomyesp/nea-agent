"""Fechas como las dice el lead → período del CRM (app/fechas.py).

El caso que lo motivó: "sábado y domingo" viajó al CRM como un solo día, a
mitad de precio, con una etiqueta que parecía decir dos.
"""
from __future__ import annotations

from app.fechas import rango_de_uso, vista_de_periodo


def test_sabado_y_domingo_son_dos_dias_y_la_maquina_vuelve_el_lunes():
    assert rango_de_uso("2026-09-12", "2026-09-13", 2) == ("2026-09-12", "2026-09-14")


def test_si_la_cuenta_no_cierra_se_frena_antes_del_crm():
    """Exactamente lo que hizo el modelo: sábado y domingo, un día."""
    out = rango_de_uso("2026-09-12", "2026-09-13", 1)
    assert isinstance(out, dict)
    assert out["error"] == "fechas_no_cierran"
    assert "son 2 días" in out["detalle"]
    assert "sábado 12/09" in out["detalle"]


def test_un_solo_dia():
    assert rango_de_uso("2026-09-12", "2026-09-12", 1) == ("2026-09-12", "2026-09-13")


def test_sin_dias_se_piden_con_la_cuenta_a_la_vista():
    out = rango_de_uso("2026-09-12", "2026-09-15", None)
    assert isinstance(out, dict)
    assert out["error"] == "faltan_datos"
    assert "son 4" in out["detalle"]


def test_dias_como_texto_o_como_float_entero():
    assert rango_de_uso("2026-09-12", "2026-09-13", "2") == ("2026-09-12", "2026-09-14")
    assert rango_de_uso("2026-09-12", "2026-09-13", 2.0) == ("2026-09-12", "2026-09-14")


def test_ultimo_dia_antes_que_desde():
    out = rango_de_uso("2026-09-13", "2026-09-12", 1)
    assert isinstance(out, dict) and out["error"] == "rango_invalido"


def test_una_fecha_que_no_es_fecha():
    out = rango_de_uso("el sábado", "2026-09-13", 2)
    assert isinstance(out, dict) and out["error"] == "faltan_datos"


def test_el_periodo_del_crm_se_muestra_con_el_ultimo_dia_de_uso():
    assert vista_de_periodo("2026-09-12", "2026-09-14") == {
        "desde": "2026-09-12",
        "ultimo_dia": "2026-09-13",
        "dias": 2,
    }


def test_el_periodo_acepta_instantes_iso_completos():
    # recien_tomada manda instantes, no fechas peladas.
    vista = vista_de_periodo("2026-10-05T00:00:00.000Z", "2026-10-12T00:00:00.000Z")
    assert vista["ultimo_dia"] == "2026-10-11"
    assert vista["dias"] == 7
