"""GKS-B1 — `skeleton.skeleton_keys` SAF birim testleri: kural (A)nın TÜM dalları ve sıra.

DB'siz saf fonksiyondur; testler yine `async`tir çünkü paketin autouse fikstürü (`_mu3d_esleme`)
asenkrondur.
"""

import uuid
from decimal import Decimal

import pytest

from app.modules.site_diary.skeleton import (
    SkeletonItem,
    SkeletonLine,
    group_allocations,
    skeleton_keys,
)

pytestmark = pytest.mark.asyncio

D = Decimal


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


A, B, C, D4 = (SkeletonItem(_id(n), D(q)) for n, q in ((1, 100), (2, 100), (3, 100), (4, 50)))
S1, S2, S3 = _id(101), _id(102), _id(103)


def _anahtar(satirlar: list[SkeletonLine]) -> list[tuple[int, int | None, Decimal]]:
    return [
        (s.item_id.int, None if s.section_id is None else s.section_id.int, s.planned)
        for s in satirlar
    ]


async def test_bolum_secili_degil_tahsissiz_kalem_tek_bolumsuz_satir_planli_kota() -> None:
    assert _anahtar(skeleton_keys([A], {}, None)) == [(1, None, D(100))]


async def test_bolum_secili_degil_kismi_tahsisli_kalem_tek_bolumsuz_planli_kalan() -> None:
    sonuc = skeleton_keys([B], {B.id: {S1: D(40)}}, None)
    assert _anahtar(sonuc) == [(2, None, D(60))]  # bölüm satırı AÇILMAZ


async def test_bolum_secili_degil_tam_tahsisli_kalem_bolumsuz_YOK_her_bolum_icin_satir() -> None:
    sonuc = skeleton_keys([C], {C.id: {S1: D(70), S2: D(30)}}, None, {S1: 0, S2: 1})
    assert _anahtar(sonuc) == [(3, 101, D(70)), (3, 102, D(30))]


async def test_tam_tahsis_savunmaci_toplam_kotayi_asarsa_da_bolumsuz_yok() -> None:
    sonuc = skeleton_keys([D4], {D4.id: {S1: D(60)}}, None)
    assert _anahtar(sonuc) == [(4, 101, D(60))]


async def test_kota_sifir_ve_tahsissiz_kalem_gorunur_kalir_bolumsuz_planli_sifir() -> None:
    sifir = SkeletonItem(_id(9), D(0))
    assert _anahtar(skeleton_keys([sifir], {}, None)) == [(9, None, D(0))]


async def test_bolum_secili_yalniz_o_bolume_tahsisli_kalemler_planli_pay_bolumsuz_yok() -> None:
    tahsis = {B.id: {S1: D(40)}, C.id: {S1: D(70), S2: D(30)}}
    sonuc = skeleton_keys([A, B, C, D4], tahsis, S1)
    assert _anahtar(sonuc) == [(2, 101, D(40)), (3, 101, D(70))]


async def test_bolum_secili_hicbir_kalem_tahsisli_degilse_bos_liste() -> None:
    assert skeleton_keys([A, B], {B.id: {S1: D(40)}}, S2) == []


async def test_bolum_satirlari_bolum_siralariyla_id_degil_sort_order() -> None:
    # id sırası S1<S2<S3; bölüm sırası S3, S1, S2 → çıktı bölüm sırasıyla.
    tahsis = {C.id: {S1: D(30), S2: D(30), S3: D(40)}}
    sonuc = skeleton_keys([C], tahsis, None, {S3: 0, S1: 1, S2: 2})
    assert [s.section_id for s in sonuc] == [S3, S1, S2]


async def test_kalem_sirasi_girdi_sirasidir_bolumsuz_ve_bolumlu_karisik() -> None:
    tahsis = {B.id: {S1: D(40)}, C.id: {S1: D(70), S2: D(30)}}
    sonuc = skeleton_keys([A, B, C, D4], tahsis, None, {S1: 0, S2: 1})
    assert _anahtar(sonuc) == [
        (1, None, D(100)),
        (2, None, D(60)),
        (3, 101, D(70)),
        (3, 102, D(30)),
        (4, None, D(50)),  # D4 tahsissiz bu haritada
    ]


async def test_group_allocations_kalem_bazli_gruplar() -> None:
    sonuc = group_allocations({(_id(1), S1): D(5), (_id(1), S2): D(6), (_id(2), S1): D(7)})
    assert sonuc == {_id(1): {S1: D(5), S2: D(6)}, _id(2): {S1: D(7)}}


async def test_her_kalem_en_az_bir_satirla_gorunur_bolum_secili_degilken() -> None:
    tahsis = {B.id: {S1: D(40)}, C.id: {S1: D(100)}, D4.id: {S2: D(50)}}
    sonuc = skeleton_keys([A, B, C, D4], tahsis, None)
    assert {s.item_id for s in sonuc} == {A.id, B.id, C.id, D4.id}
