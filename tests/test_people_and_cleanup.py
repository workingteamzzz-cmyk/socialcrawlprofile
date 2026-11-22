"""INFLUENCER / PERSON rules and the 'Baca juga' cleanup (the team's Online columns)."""
from crawler.medmas.extract import clean_content
from crawler.medmas.persons import extract_people

ARTICLE = '''Menko Perekonomian Airlangga Hartarto menghadiri pertemuan dengan European Commission di Brussels.
"Kerja sama ini penting bagi ekspor kita," kata Airlangga.
Presiden Amerika Serikat Donald Trump juga disebut dalam pertemuan itu. Menurut Trump, tarif akan turun.
Ekonom Goldman Sachs memperkirakan pertumbuhan naik. Kepala Dinas Perdagangan Budi Santoso mengatakan bahwa ekspor naik.
Suroso Isnandar menegaskan, "Pasokan listrik aman sampai akhir tahun."
Pertemuan Indonesia-European Union itu berlangsung di Beijing pekan lalu.'''


def test_influencer_needs_a_quotation_and_a_speech_verb():
    speakers, _ = extract_people(ARTICLE)
    assert speakers == ["Airlangga Hartarto", "Suroso Isnandar"]      # Budi / Trump: indirect speech only


def test_person_is_every_named_person_in_full_never_an_organisation_or_place():
    _, persons = extract_people(ARTICLE)
    assert persons == ["Airlangga Hartarto", "Donald Trump", "Budi Santoso", "Suroso Isnandar"]
    low = " ".join(persons).lower()
    for bad in ("european", "goldman", "sachs", "beijing", "perekonomian", "serikat", "pasokan"):
        assert bad not in low


def test_baca_juga_blocks_go_real_sentences_stay():
    t = """Pemerintah menaikkan anggaran subsidi listrik tahun depan, kata menteri dalam rapat di DPR hari ini.
BACA JUGA:
PLN Padamkan Listrik di Bogor Selama Enam Jam
Harga BBM Naik Lagi Mulai Besok
Anggaran itu naik dua kali lipat, seperti yang bisa dibaca juga dalam laporan BPK yang dirilis pekan lalu.
Petugas melihat juga kondisi gardu di lokasi sebelum perbaikan dimulai pagi tadi.
Warga menyambut baik kebijakan itu karena tarif tidak naik. Baca: Tarif Listrik Tetap. Menurut warga, ini kabar baik."""
    out = clean_content(t)
    assert "PLN Padamkan" not in out and "Harga BBM" not in out and "Tarif Listrik Tetap" not in out
    assert "dibaca juga dalam laporan BPK" in out                          # an ordinary sentence is untouched
    assert "melihat juga kondisi gardu" in out
    assert "Menurut warga, ini kabar baik." in out


def test_a_subheading_elsewhere_stays_and_the_block_stops_at_the_first_paragraph():
    t = """Paragraf pembuka berita yang cukup panjang tentang kebijakan energi nasional tahun depan.
Dampak bagi Warga
Berita Terkait
Banjir Rendam Ribuan Rumah
Paragraf berikutnya yang juga panjang menjelaskan dampak kebijakan bagi rumah tangga miskin di desa.
Penutup"""
    out = clean_content(t)
    assert "Dampak bagi Warga" in out                                     # not under a block header: kept
    assert "Banjir Rendam" not in out
    assert "Paragraf berikutnya" in out and "Penutup" in out


def test_safety_brake_never_takes_much_of_an_article():
    """A page whose lines are almost all short lines under a 'Baca juga' header: the block removal is not applied."""
    lines = ["Baca juga:"] + [f"Baris pendek nomor {i} tanpa titik" for i in range(5)] + ["Kalimat penutup singkat."]
    out = clean_content("\n".join(lines))
    assert "Baris pendek nomor 0" in out


INTRO = "Pemerintah memastikan pasokan listrik aman selama libur akhir tahun di seluruh wilayah Indonesia, kata pejabat.\n" * 6


def test_short_real_sentences_and_quotes_under_a_header_are_kept():
    t = INTRO + ("Baca Juga:\nMenteri ESDM Bahlil Lahadalia menyebut pasokan aman.\n"
                 "\"Kami siap menghadapi lonjakan,\" kata Bahlil.\nBeban puncak naik 5 persen.")
    out = clean_content(t)
    assert "Bahlil Lahadalia menyebut" in out and "Kami siap menghadapi lonjakan" in out and "naik 5 persen" in out


def test_real_paragraphs_that_start_like_markers_are_kept():
    for line in ("Terpopuler di kalangan warga, aplikasi ini diunduh jutaan kali.",
                 "Baca berita ini dengan teliti sebelum membagikannya, ujar Kapolres.",
                 "Read more about the policy here.",
                 "Warga diminta baca: aturan baru berlaku mulai besok tanpa pengecualian",
                 "Program bertajuk Ayo Baca: Gerakan Membaca Nasional diluncurkan Menteri Pendidikan.",
                 "Ia mengaku sering baca juga buku sejarah di perpustakaan kota."):
        assert clean_content(line) == line
    assert clean_content("Baca selengkapnya: Harga Emas Hari Ini Naik") == ""


def test_names_are_kept_whole_even_with_place_words_inside():
    for text, name in (('Pengacara Hotman Paris Hutapea mengatakan, "Kami siap membela klien kami."', "Hotman Paris Hutapea"),
                       ('Kepala Dinas Kesehatan dr. Uni Kurniasih mengatakan, "Vaksin sudah tersedia di puskesmas."', "Uni Kurniasih"),
                       ('Ketua DPRD Agus Asia Pratama menegaskan, "Anggaran harus tepat sasaran tahun ini."', "Agus Asia Pratama"),
                       ('Kapolres AKBP Timor Silalahi menyatakan, "Situasi kondusif di seluruh wilayah."', "Timor Silalahi"),
                       ('Presiden Rusia Vladimir Putin menegaskan, "Kami terbuka untuk dialog damai."', "Vladimir Putin")):
        assert extract_people(text) == ([name], [name])
    assert extract_people('Ketua panitia, Negara Sitepu, menjelaskan acara berlangsung meriah.')[1] == ["Negara Sitepu"]


def test_who_speaks_and_who_is_only_mentioned():
    sp, pe = extract_people('"Presiden Prabowo Subianto meminta kami siap siaga," kata Kapolda Irjen Ahmad Luthfi.')
    assert sp == ["Ahmad Luthfi"] and pe == ["Prabowo Subianto", "Ahmad Luthfi"]      # named inside a quote: mentioned
    sp, _ = extract_people('Menteri Kesehatan Budi Gunadi Sadikin hadir. Warga bernama Budi Santoso ikut antre. '
                           '"Kami senang sekali hari ini," kata Budi.')
    assert sp == ["Budi Santoso"]                                                     # the Budi mentioned last
    assert extract_people('"Kami siap bekerja keras." Ia lalu pergi. Budi Santoso mengatakan harga beras naik.')[0] == []
    assert extract_people('Dalam film "Gundala", sutradara Joko Anwar mengatakan proses syuting berlangsung lama.')[0] == []
    assert extract_people('Bahlil Lahadalia menuturkan: "Pasokan BBM aman sampai akhir tahun."') == \
        (["Bahlil Lahadalia"], ["Bahlil Lahadalia"])
    assert extract_people('Seluruh pejabat hadir. Rapat dimulai pukul sembilan pagi.') == ([], [])
    assert extract_people('Rumah Sakit Fatmawati menambah ruang rawat. Direktur Fatmawati menyebut kapasitas cukup.') == ([], [])
    assert extract_people("") == ([], []) and extract_people(None) == ([], [])
