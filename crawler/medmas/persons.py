"""Rule-based person-name extraction for Indonesian news text (NER-lite).

Definitions (matching the media-monitoring upload template):
- INFLUENCER: people who SPEAK in the article (quote attribution:
  "ujar Iwan", "Kendi menyampaikan", "menurut Budi", ...).
- PERSON: every person mentioned by name, speaking or not.

The extractor works without heavy ML models:
1. Find capitalized-word sequences as candidates.
2. Strip honorifics (H., Dr., Pak, ...) and role titles (Kepala Dinas,
   Presiden, Manager, ...) from the front — the actual name follows them.
3. Reject candidates containing organization / place / time markers
   (PT, Dinas, Kota, Kecamatan, Senin, Januari, ...).
4. Reject phrases whose words also appear lowercase in the text
   (e.g. "Jaringan Tegangan Rendah" — capitalized terms, not names).
5. Detect speakers via an extensive Indonesian speech-verb list on both
   sides of the name ("kata X" and "X mengatakan").
Single-word names need supporting context (speech verb, honorific, role,
"menurut", or age pattern) or repeated occurrence to be accepted.
"""

import re

# Indonesian speech verbs — base forms and me-/ber- derivations
SPEECH_VERBS = frozenset("""
ujar kata ucap tutur jelas ungkap tegas papar terang imbuh sebut pungkas
beber tandas lanjut tambah harap pinta ajak nilai aku klaim tanya jawab
keluh pesan tekan
mengatakan menyampaikan menjelaskan menegaskan menuturkan mengungkapkan
mengungkap menambahkan melanjutkan mengimbau menghimbau meminta memaparkan
menerangkan menyebutkan menyebut menilai mengklaim mengaku mengakui
mengeluhkan menanyakan menjawab membeberkan menandaskan mengajak
menyatakan memastikan menargetkan mendorong menekankan mengapresiasi
memperkirakan mengingatkan menuding menuduh membantah menepis mengkritik
menyoroti mempertanyakan menginstruksikan memerintahkan melaporkan
mengumumkan menuturkan menghargai menyesalkan mendesak menghimbau
berujar berkata bertutur berpesan berharap berpendapat berkomentar
mengemukakan menyarankan mengusulkan menyerukan menggarisbawahi
menyimpulkan menjabarkan merinci mengonfirmasi membenarkan menampik
menyindir menyinggung berkisah bercerita menjamin menyanggah meluruskan
menegur mengajukan memproyeksikan menyampaikannya menandaskan
""".split())

# Honorifics stripped from the front of a candidate
HONORIFICS = frozenset("""
bapak ibu pak bu saudara saudari sdr sdri h hj kh dr drh ir prof drs dra
ust ustaz ustadz ustadzah gus kiai kyai habib raden tuan nyonya nona mas
mbak bang kang mr mrs ms sir madam
""".split())

# Role/title words stripped from the front — a person name usually follows
ROLE_WORDS = frozenset("""
presiden wakil menteri menko wamen gubernur wagub bupati wabup wali
walikota camat lurah kepala ketua sekretaris sekjen bendahara direktur
dirut dirjen manager manajer komisaris komisioner kapolri kapolda kapolres
kapolsek kasat kanit kajari kajati jaksa hakim panitera danramil dandim
pangdam danrem komandan rektor dekan koordinator kordinator juru bicara
anggota staf plt pj pjs kadis kabid kasi kabag sekda asisten penjabat
pelaksana tugas pengurus pembina pimpinan officer chief executive general
head spokesperson penasihat supervisor pengamat analis ekonom pakar
politisi legislator senator aktivis atlet artis musisi sutradara
pengusaha tokoh dokter perawat bidan guru dosen siswa siswi mahasiswa
mahasiswi karyawan petani nelayan sopir pedagang seniman penyanyi aktor
aktris pelatih pemain korban tersangka terdakwa saksi pelaku terpidana
bocah pemuda pemudi remaja pria wanita lelaki perempuan kakek nenek
almarhum almarhumah ustadzah pendeta pastor biksu
desainer perancang arsitek insinyur peneliti ilmuwan wartawan jurnalis
fotografer presenter pembalap pebisnis youtuber selebgram kreator
produser komedian pelawak pengacara advokat notaris psikolog pilot chef
koki perajin pengrajin penceramah dai mubaligh
perekonomian maritim investasi politik keamanan polhukam pembangunan manusia
pertahanan perencanaan agraria transmigrasi sekretariat negara luar negeri
dalam kabinet koordinator bidang urusan hubungan pemberantasan penanggulangan
kesehatan pendidikan perhubungan pertanian keuangan kominfo komunikasi
informatika sosial pariwisata perdagangan perindustrian ketenagakerjaan
kebudayaan olahraga kepemudaan lingkungan kehutanan perikanan kelautan
peternakan perkebunan pekerjaan umum penataan perumahan permukiman
kependudukan pencatatan perpustakaan kearsipan penanaman pemberdayaan
perlindungan pengendalian ketahanan pangan koperasi perizinan
kesbangpol humas hukum agama pertanahan pendapatan pengelolaan
""".split())

# Left-context words that signal "a person's name follows"
CONTEXT_WORDS = frozenset("""
bersama oleh bernama dipimpin dihadiri didampingi diwakili mewakili
menggandeng menemui menjenguk menghubungi sosok yakni yaitu
hadir menghadirkan menugaskan menunjuk melantik mengangkat menyambut
menerima memanggil memperkenalkan menggantikan almarhum almarhumah
""".split())

# Sentence-connective words: stripped from the FRONT of a candidate
# ("Menurut Dedi" -> "Dedi"), never proof of non-person by themselves.
FRONT_STOPWORDS = frozenset("""
namun selain sementara sebelumnya kemudian adapun bahkan meski meskipun
walau walaupun sejak pada dalam untuk dengan ketika saat setelah hingga
karena jika apabila menurut berdasarkan terkait seperti sedangkan akibat
akibatnya sehingga kepada tentang antara melalui terhadap secara serta
""".split())

# Tokens that mark a candidate as NOT a person (place / org / time / common)
NON_PERSON = frozenset("""
senin selasa rabu kamis jumat sabtu minggu
januari februari maret april mei juni juli agustus september oktober
november desember
kota kabupaten kecamatan kelurahan desa dusun provinsi jalan jl kampung
gang blok perumahan komplek kompleks rt rw nusantara indonesia
jawa sumatera sumatra kalimantan sulawesi papua bali maluku aceh banten
jakarta yogyakarta jogja barat timur utara selatan tengah pusat tenggara
pt cv ud tbk persero dinas badan balai kementerian kemenko polres polsek
polda polri tni kodim koramil kejaksaan pengadilan mahkamah universitas
institut sekolah akademi sma smk smp sd mts man min bank dprd dpr mpr dpd
kpu kpk bumn bumd rsud rsu rs puskesmas pln pdam pemkot pemkab pemprov
pemerintah satpol yayasan lembaga komisi fraksi partai asosiasi persatuan
perhimpunan ikatan forum komunitas paguyuban koperasi perusahaan perseroan
media tim unit layanan pelanggan ulp upt uptd cabang wilayah daerah
nasional internasional group grup holding corp persada karya utama
warga masyarakat pihak hal ini itu the markas posko tanah air negeri
atau dan yang di ke dari
commission council union european europe eropa uni asean apec g20 who imf
unesco unicef nato opec sachs morgan stanley capital fund trust inc ltd llc
co company corporation corp limited plc ag gmbh foundation institute agency
ministry office department authority federation association society club
party news times post daily herald tribune journal tv radio online channel
network digital tech technology technologies global international energy
mining motor motors airlines airways air bank securities sekuritas finance
group holdings ventures partners industries exchange market index
serikat republik kerajaan federasi china tiongkok cina beijing shanghai amerika america american usa as washington
inggris london britania eropa prancis perancis paris jerman berlin rusia moskow
jepang tokyo korea seoul pyongyang india australia malaysia singapura singapore
thailand bangkok vietnam filipina manila myanmar kamboja laos brunei timor
arab saudi mesir iran irak israel palestina gaza ukraina turki belanda italia
spanyol kanada brasil meksiko afrika asia pasifik timur tengah
""".split())

# Markers whose following capitalized word is a PLACE name — used to LEARN
# local place names from the article itself ("Kabupaten Nunukan" teaches
# the extractor that "Nunukan" is a place, not a person).
_PLACE_MARKER_RE = re.compile(
    r"\b(?:Kabupaten|Kota|Kecamatan|Kelurahan|Desa|Dusun|Provinsi|Kampung|"
    r"Pulau|Gunung|Sungai|Danau|Bandara|Pelabuhan)\s+([A-Z][a-zA-Z]+)")
_PLACE_PREP_RE = re.compile(r"\b(?:di|ke)\s+([A-Z][a-zA-Z]+)\b")
_SPEECH_NAME_RE = re.compile(
    r"(?:ujar|kata|ucap|tutur|jelas|ungkap|tegas|papar|imbuh|sebut|pungkas|"
    r"beber|tandas|lanjut|tambah|harap|menurut)(?:nya)?\s+([A-Z][a-zA-Z]+)")


def _learn_places(text):
    """Collect place names taught by the text itself (lowercased).
    Names directly attached to a speech verb are protected — they are
    people, even if they once appear after 'di'/'ke'."""
    places = {m.group(1).lower() for m in _PLACE_MARKER_RE.finditer(text)}
    places |= {m.group(1).lower() for m in _PLACE_PREP_RE.finditer(text)}
    places -= {m.group(1).lower() for m in _SPEECH_NAME_RE.finditer(text)}
    return places

NAME_PARTICLES = frozenset("bin binti al el van der da de abdul abd".split())

# Capitalized-word sequence, allowing initials (A.) and name particles
_CAND_RE = re.compile(
    r"\b((?:[A-Z][a-zA-Z'’\-]+|[A-Z]\.)"
    r"(?:[ ](?:bin|binti|al|el|van|der|abdul|[A-Z][a-zA-Z'’\-]+|[A-Z]\.)){0,7})")

_WORD_RE = re.compile(r"[A-Za-z]+")


def _strip_front(tokens, places=frozenset()):
    """Strip honorifics, role titles, connectives, speech verbs, ALL-CAPS
    org abbreviations, and learned place names from the front
    ("Menteri ESDM Bahlil" -> "Bahlil", "Menurut Dedi" -> "Dedi",
    "Ketua PMI Kabupaten Nunukan Saddam Husein" -> "Saddam Husein").
    Return (kept_tokens, stripped_prefix_tokens)."""
    i = 0
    saw_role = False
    while i < len(tokens):
        raw = tokens[i]
        t = raw.rstrip(".").lower()
        if t in HONORIFICS or t in FRONT_STOPWORDS or t in SPEECH_VERBS:
            i += 1
        elif t in ROLE_WORDS:
            saw_role = True
            i += 1
        elif raw.isupper() and 2 <= len(raw) <= 6 and "." not in raw:
            i += 1  # org abbreviation like ESDM, PUPR, DPRD
        elif t in places:
            i += 1  # place name learned from the article itself
        elif saw_role and t in NON_PERSON:
            i += 1  # "Sekretaris *Daerah* Kusnadi" — part of the role phrase
        else:
            break
    return tokens[i:], tokens[:i]


def _add(daftar, nama):
    if nama.lower() not in (x.lower() for x in daftar):
        daftar.append(nama)


def extract_people(text):
    """Return (influencers, persons): influencers = quoted speakers,
    persons = all named people (superset of influencers)."""
    if not text:
        return [], []
    persons, speakers = [], []

    # lowercase vocabulary of the text — capitalized words that also appear
    # lowercase are ordinary words, not names
    lower_words = set(re.findall(r"(?<![A-Z])\b([a-z][a-z'’\-]{2,})\b", text))
    # place names the article itself teaches ("Kabupaten Nunukan", "di Sebatik")
    places = _learn_places(text)

    candidates = []
    for m in _CAND_RE.finditer(text):
        raw, s, e = m.group(1), m.start(1), m.end(1)
        tokens, prefix = _strip_front(raw.split(), places)
        if not tokens:
            continue
        low = [t.rstrip(".").lower() for t in tokens]
        parts = [p for t in low for p in re.split(r"[-’']", t) if p]
        if any(t in NON_PERSON or t in places for t in low) or any(p in NON_PERSON for p in parts):
            continue                       # "Indonesia-European", "Goldman Sachs", "Beijing": not people
        if any(re.search(r"\d", t) for t in tokens):
            continue
        # words of the "name" that exist lowercase in text -> capitalized term
        if any(t in lower_words for t in low if len(t) > 2 and t not in NAME_PARTICLES):
            continue
        name = " ".join(tokens).strip("-’' ")
        if len(name.replace(".", "").replace(" ", "")) < 3:
            continue
        left, right = text[max(0, s - 45):s], text[e:e + 45]
        if left.rstrip(" ").endswith(tuple(_QUOTES)):
            continue                       # '"Pasokan listrik aman…"': the quotation's first word, not a name
        # followed by an ALL-CAPS abbreviation in parentheses -> a term
        if re.match(r"\s*\([A-Z]{2,}\)", right):
            continue
        prefix_low = [t.rstrip(".").lower() for t in prefix]
        # a company marker in the stripped prefix ("Direktur PT Maju Jaya")
        # means what follows is the COMPANY name, not a person
        if any(t in ("pt", "cv", "ud", "persero", "tbk") for t in prefix_low):
            continue
        candidates.append((name, tokens, left, right, prefix, prefix_low, s, e))

    def is_speaker(left, right):
        # only the word IMMEDIATELY before the name counts ("ujar Iwan"),
        # otherwise a verb from a previous sentence causes false speakers
        lw = _WORD_RE.findall(left.lower())
        if lw and (lw[-1] in SPEECH_VERBS or lw[-1] == "menurut"):
            return True
        rm = re.match(r"[ ,]*(?:\(\d{1,3}\)[ ,]*)?(?:\d{1,3} tahun[ ,]*)?"
                      r"(?:pun |juga |kembali |sempat |lantas |kemudian )?([a-z]+)",
                      right.lower())
        return bool(rm and rm.group(1) in SPEECH_VERBS)   # "Iwan mengatakan"

    for name, tokens, left, right, prefix, prefix_low, s, e in candidates:
        # a speech verb / "menurut" stripped from the candidate's own front
        # ("Menurut Dedi", "Kata Budi") also marks a speaker
        speaking = (is_speaker(left, right)
                    or any(t in SPEECH_VERBS or t == "menurut" for t in prefix_low))
        lw = [w.rstrip(".,").lower() for w in left.strip().split()]
        prev = lw[-1] if lw else ""
        # apposition after a role phrase: "Ketua PMI Nunukan, Saddam Husein, ..."
        apposition = (left.rstrip().endswith(",")
                      and any(w in ROLE_WORDS or w in HONORIFICS for w in lw[-6:]))
        # A role in the candidate's own prefix ("Presiden Prabowo Subianto")
        # is evidence — EXCEPT when an org abbreviation was stripped too and
        # only one token remains ("Manager ULP Cipayung" -> Cipayung is the
        # unit's place, not a person; the real person follows in apposition).
        has_abbrev = any(t.isupper() and 2 <= len(t) <= 6 for t in prefix)
        prefix_role = (any(t in HONORIFICS or t in ROLE_WORDS for t in prefix_low)
                       and (len(tokens) >= 2 or not has_abbrev))
        # Same trap with a speech verb: "Manager ULP Cipayung menjelaskan"
        # — the single leftover after an org abbreviation is the unit's
        # place, not the speaker. Demand a second token or no abbreviation.
        if speaking and has_abbrev and len(tokens) < 2 and not (
                prev in HONORIFICS or prev in CONTEXT_WORDS):
            speaking = False
        # STRICT rule: a name is only accepted with person-evidence.
        # Capitalized phrases without evidence (section headings, program
        # names, "Tanah Air") are rejected — no more random entries.
        has_context = (speaking
                       or prev in HONORIFICS or prev in ROLE_WORDS
                       or prev in CONTEXT_WORDS or prev == "menurut"
                       or apposition
                       or prefix_role
                       or bool(re.match(r"\s*\(\d{1,2}\)", right))          # Budi (34)
                       or bool(re.match(r",?\s*\d{1,2} tahun", right)))     # Budi, 34 tahun
        if has_context:
            _add(persons, name)
            if speaking and _quoted(text, s, e):
                _add(speakers, name)       # an INFLUENCER speaks: a speech verb AND a quotation

    # the fullest form the article writes: "Airlangga" -> "Airlangga Hartarto", "Trump" -> "Donald Trump"
    forms = [c[0] for c in candidates
             if not any(t.rstrip(".").lower() in ROLE_WORDS or t.rstrip(".").lower() in NON_PERSON
                        for t in c[0].split())]
    speakers = _dedupe([_fullest(n, forms) for n in speakers])
    persons = _dedupe([_fullest(n, forms) for n in persons])
    # a speaker is always also a person
    for sp in speakers:
        _add(persons, sp)
    return speakers[:15], persons[:25]


_QUOTES = "\"“”„«»"


def _quoted(text, s, e):
    """The name stands by a quotation: '"…," kata Budi' (a quote closes just before) or 'Budi mengatakan, "…"'
    (a quote opens in the same sentence after) — indirect speech without quotation marks is not enough."""
    left = text[max(0, s - 90):s]
    q = max(left.rfind(c) for c in _QUOTES)
    if q >= 0 and not re.search(r"[.!?]\s+[A-Z]", left[q + 1:]):
        return True                          # the quotation ends in this sentence, right before the name
    right = text[e:e + 220]
    qs = [i for i in (right.find(c) for c in _QUOTES) if i >= 0]
    if qs:
        first = min(qs)
        end = re.search(r"[.!?]\s", right)
        return end is None or first < end.start()
    return False


def _fullest(name, forms):
    """The longest name in the article that contains this one word for word (at most 4 words)."""
    toks = name.lower().split()
    best = name
    for f in forms:
        ft = f.lower().split()
        if len(ft) <= len(best.split()) or len(ft) > 4:
            continue
        if any(ft[i:i + len(toks)] == toks for i in range(len(ft) - len(toks) + 1)):
            best = f
    return best


def _dedupe(names):
    out = []
    for n in names:
        _add(out, n)
    return out
