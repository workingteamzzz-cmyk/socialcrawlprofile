"""INFLUENCER / PERSON clean-up: the people detector of the news engine also catches pronouns ("kami", "saya"),
ordinary words ("menurutnya", "lebih"), titles without a name ("irjen pol", "kapolresta"), institutions and brands
("kostrad", "pertamina", "facebook") and glues ranks, places and units in front of a name ("sumsel irjen pol rony
samtana", "rusia vladimir putin"). This keeps the person: "rony samtana", "vladimir putin" — and drops the rest.
Applied to every Online row (stream, News Search, reports), the engine's own code unchanged."""
from __future__ import annotations

import re

# words that are never a person on their own
NOT_PEOPLE = set("""
saya aku kami kita dia ia mereka beliau anda engkau kamu kalian sang si para pak bu bapak ibu mas mbak bang kak
yang dan atau dengan untuk dari pada dalam oleh akan juga lebih atas bawah salah benar sangat sudah belum masih
periksa bertujuan merekrut modal sipil perekonomian ekonomi usaha kecil menengah besar baru lama tersebut ini itu
sementara sedangkan namun tetapi karena sehingga agar supaya bahwa kalau jika ketika saat setelah sebelum selama
semua seluruh setiap beberapa banyak sedikit pertama kedua ketiga terakhir hal cara kata ujar tutur jelas tegas ucap
sebut kata-nya menyatakan mengatakan menjelaskan menegaskan mengungkapkan menyebut menurut terkait soal tentang
pemerintah masyarakat warga rakyat negara daerah wilayah kota kabupaten desa kampung provinsi pusat nasional
internasional dunia indonesia nusantara bahasa inggris arab sumber tim redaksi admin humas sihumas counsel suite
pancasila kitab hukum undang perda program kegiatan acara rapat sidang pertemuan konferensi pers berita info
foundation instruksi comm ppa ahli alam berkah bila fokus islam khusus kiprah layak lega luas medik narkoba ops opsi
padahal pewarta putih region sebab semangat terlebih upaya usai thread raperda keempat kelima keenam juara jum’at
jumat sabtu minggu senin selasa rabu kamis menjaga menjadikan mudah-mudahan statistik riset penyidik inspektorat
call technology winter incidental champions league official resmi reputasi rentetan tekanan ukuran pilihan
penyakit germany log punitive panit rel ren ras muh muda multimoda bumdes komlekdam tanfidziyah baitulmal syuriyah
bandung jakarta surabaya medan makassar semarang palembang bekasi depok bogor tangerang tangsel malang solo
denpasar batam pekanbaru padang manado balikpapan samarinda pontianak banjarmasin kupang mataram ambon jayapura
malinau nunukan bangka beltim belitung kendari palu mamuju ternate sorong merauke cirebon tasikmalaya garut sukabumi
""".split())
# institutions, units, brands — dropped when they are the whole "name"
ORGS = set("""
kostrad kopassus kemhan kemenhan tni polri polda polres polresta polsek bappeda bappenas bpbd bnpb bmkg bpk kpk kpu
bawaslu dpr dpd dprd mpr ma mk kejagung kejati kejari kemenkes kemendikbud kemenag kemenkeu kemendagri kemenlu
kementan kemenpupr pupr pln pertamina telkom bri bni btn mandiri bca bsi jne jnt pos ojk bi bps bpjs garuda
facebook google instagram tiktok youtube whatsapp twitter meta apple samsung opensignal microsoft amazon shopee
tokopedia gojek grab traveloka pemkot pemkab pemprov setda satpol damkar basarnas bulog wwf unicef who pbb asean
chelsea milan persib persija arema fulham liverpool barcelona madrid juventus rossoneri rostec citigroup honda
toyota yamaha suzuki rockstar elnusa danantara moody’s moodys brandwatch netflix spotify xiaomi oppo vivo
satresnarkoba satreskrim komcad rindam latsarmil inspektorat imigrasi dinkes dikpora kemenpar kemkomdigi bapenda
""".split())
# a word that starts an institution / unit: "lapas kotabaru", "kamar dagang", "divisi infanteri"
ORG_HEADS = set("""
lapas rutan bidang kamar divisi dinas badan kementerian kantor bank pt cv universitas univ institut sekolah sma smk
smp sd partai komisi fraksi polres polresta polda polsek kodim korem kodam koramil satuan satgas unit direktorat
biro bagian seksi sub subdit balai lembaga yayasan perusahaan koperasi rumah rs puskesmas klinik pengadilan
kejaksaan kepolisian majelis dewan panitia tim grup group asosiasi persatuan ikatan himpunan forum gerakan simpang tanjung kramat jalan jl pasar kelurahan kecamatan pulau gunung sungai kawasan plaza hotel
kopi toko mapolres mapolda markas angkatan komponen kemitraan koalisi branch office area nagari plaza bunda
""".split())
# titles, ranks, roles, corps — stripped from the front ("irjen pol x" -> x); a name after the last one is the person
TITLES = set("""
presiden wapres wakil menteri menko mensesneg gubernur wagub bupati wabup wali walikota wawali camat lurah kades
kepala ketua wakil sekretaris sekjen sekda bendahara direktur dirut direksi komisaris manajer manager general senior
vice president ceo cfo coo cto founder owner chairman pimpinan pemimpin koordinator kordinator juru bicara jubir
staf staff anggota pengamat peneliti dosen guru rektor dekan dokter dr drg ir prof h hj kh ust ustaz ustadz habib
gus kyai kiai pendeta romo ustadzah syekh marketing humas kasubbag kabag kasat kapolres kapolda kapolresta
kapolsek wakapolres wakapolda wakapolresta kasatreskrim kasatlantas kanit kapuspen kadispen danrem dandim pangdam
pangkostrad kasad kasal kasau panglima kapolri wakapolri kabareskrim jaksa kajari kajati jaro
menaker menekraf menpar wamenpar wamenperin wamen kakomlekdam wakakomlekdam prokopim kaban kasubbid kasubdit kabid
kasie kasi rais aam pemasaran keuangan operasional energi pertahanan analisis komunikasi sdm eropa cke
jenderal jendral letjen mayjen brigjen kolonel kol letkol mayor kapten lettu letda serma serka sertu serda
pelda praka pratu kopda koptu irjen kombes akbp kompol akp iptu ipda aiptu aipda bripka brigpol brigadir bripda
briptu polisi pol inf arm kav czi chb cpm cba cku czi kes tni marsekal laksamana laksda laksma marsda marsma
""".split())
PLACES = set("""
rusia china tiongkok amerika serikat as inggris jepang korea selatan utara india australia prancis perancis jerman
ukraina israel palestina iran irak arab saudi malaysia singapura filipina thailand vietnam turki mesir belanda
italia spanyol brasil kanada meksiko qatar uea pakistan afghanistan suriah myanmar kamboja laos ri
aceh sumut sumbar sumsel riau kepri jambi bengkulu lampung babel banten dki jakarta jabar jateng jatim diy
yogyakarta bali ntb ntt kalbar kalteng kalsel kaltim kaltara sulut sulteng sulsel sultra sulbar gorontalo maluku
malut papua pabar barelang reskrim reserse kriminal lantas intelkam binmas sabhara samapta propam humas
""".split())


_AFFIXED = re.compile(r"^(pe|per|pen|pem|peng|peny|ke)[a-z]{3,}an$|^[a-z]{3,}(isasi|asi)$|"
                      r"^(me|mem|men|meng|meny|ber|ter|di)[a-z]{3,}(kan|i|nya)$")   # pertumbuhan, administrasi, menjadikan


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"\s+", name.strip()) if t]


def clean_one(name: str) -> str:
    """One detected name -> the person's name, or "" when it is not a person."""
    t = _tokens(name)
    low = [x.lower().strip(".,;:()") for x in t]
    if not t:
        return ""
    # a rank / title in the middle: the person is what follows the last one ("... kompol agung tri poerbowo")
    last = max((i for i, w in enumerate(low) if w in TITLES), default=-1)
    if 0 <= last < len(low) - 1:
        t, low = t[last + 1:], low[last + 1:]
    # front words that are titles, places, units, ordinary words or an org with a hyphen ("wwf-indonesia aditya")
    org_cut = False
    while len(low) > 1 and (low[0] in TITLES or low[0] in PLACES or low[0] in ORGS or low[0] in NOT_PEOPLE
                            or ("-" in low[0] and low[0].split("-")[-1] in PLACES | {"indonesia"})):
        org_cut = org_cut or low[0] in ORGS or low[0] in PLACES
        t, low = t[1:], low[1:]
    if org_cut and len(low) == 1:
        return ""                                       # "honda scoopy", "pertamina foundation": not a person
    while len(low) > 1 and low[-1] in TITLES:           # "x pol" at the end
        t, low = t[:-1], low[:-1]
    if not low:
        return ""
    if _AFFIXED.match(low[0]):
        return ""                                       # an ordinary word, not a name
    if all(w in TITLES or w in PLACES or w in ORGS for w in low):
        return ""                                       # "irjen pol", "kapolresta", "kostrad"
    if low[0] in ORG_HEADS:
        return ""                                       # "lapas kotabaru", "kamar dagang"
    if len(low) == 1:
        w = low[0]
        if w in NOT_PEOPLE or w in ORGS or w in PLACES or w in TITLES or w.endswith("nya") or len(w) < 3 \
                or "-" in w or not re.fullmatch(r"[a-z’']+", w):
            return ""
    if any(w in NOT_PEOPLE and w not in ("bin", "binti") for w in low) and len(low) <= 2:
        return ""                                       # "bahasa inggris", "usaha kecil"
    return " ".join(t)


def clean_names(names) -> str:
    """"A, B, C" -> the same list with only real people (order kept, duplicates removed)."""
    out, seen = [], set()
    for n in str(names or "").split(","):
        c = clean_one(n)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return ", ".join(out)
