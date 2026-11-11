"""Rule-based Indonesian ORGANIZATION extraction for the upload template.

Same philosophy as the person extractor: no ML downloads, tuned for
Indonesian news prose. An organization is accepted when it matches one of:

1. An institutional PREFIX followed by capitalized words:
   "Kementerian Energi dan Sumber Daya Mineral", "Polres Cilegon",
   "Universitas Gadjah Mada", "Bank Indonesia", "Dinas Kesehatan Kota
   Bogor", "Komite Olimpiade Indonesia".
2. A company marker: "PT Bukit Asam Tbk", "CV Maju Jaya",
   "Perum Bulog", "PT PLN (Persero)".
3. A well-known standalone name/acronym from the curated list
   (PLN, KPK, OJK, Polri, TNI, DPR, NU, Muhammadiyah, ...).
4. An acronym in parentheses right after an accepted name:
   "Komite Olimpiade Indonesia (NOC Indonesia)" also yields
   "NOC Indonesia".

Everything ambiguous is left out — a clean, short ORGANIZATION cell beats
a noisy one on a client dashboard.
"""

import re

# Institutional words that START an organization name (longest first).
_PREFIXES = [
    "Kementerian Koordinator", "Kementerian", "Kemenko",
    "Direktorat Jenderal", "Direktorat", "Sekretariat",
    "Dinas", "Badan", "Lembaga", "Komisi", "Komite", "Dewan", "Otoritas",
    "Asosiasi", "Yayasan", "Federasi", "Ikatan", "Perhimpunan", "Himpunan",
    "Persatuan", "Perkumpulan", "Forum", "Aliansi", "Koalisi", "Gabungan",
    "Universitas", "Institut", "Politeknik", "Sekolah Tinggi", "Akademi",
    "Bank", "Bursa Efek",
    "Rumah Sakit Umum Daerah", "Rumah Sakit", "RSUD", "RSUP", "RSU",
    "Puskesmas", "Balai",
    "Pemerintah Kota", "Pemerintah Kabupaten", "Pemerintah Provinsi",
    "Pemkot", "Pemkab", "Pemprov", "Pemdes",
    "Polda", "Polrestabes", "Polresta", "Polres", "Polsek",
    "Kejaksaan Agung", "Kejaksaan Negeri", "Kejaksaan Tinggi",
    "Kejaksaan", "Kejari", "Kejati",
    "Pengadilan Negeri", "Pengadilan Tinggi", "Pengadilan Agama",
    "Pengadilan", "Mahkamah",
    "Kodam", "Korem", "Kodim", "Koramil", "Lanud", "Lanal",
    "Satpol PP", "Partai", "Olympic Council of",
    "Kantor Wilayah", "Kanwil", "Perusahaan Daerah", "Perusda",
]
_PREFIX_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _PREFIXES) + r")"
    r"((?:\s+(?:[A-Z][\w’'.-]*|dan|of|the|untuk|Asia|Indonesia))+)")

# Company markers: the marker alone is never an org — the name after it is.
_COMPANY_RE = re.compile(
    r"\b(PT|CV|Perum|Perumda|Koperasi)\s+"
    r"((?:[A-Z][\w’'.-]*\s*)+?(?:Tbk|\(Persero\))?)(?=[\s,.;:)]|$)")

# ==================================================================
# BUILT-IN ORGANIZATION DICTIONARY — well-known standalone names and
# acronyms, matched as whole words (case-sensitive: 'BI' != 'bi').
# Extend it with your own file: put organization_dictionary.txt (or
# kamus_organisasi.txt) in the app folder, one name per line — it is
# loaded automatically on first use.
# ==================================================================
_KNOWN = [
    # --- state institutions & agencies ---
    "KPK", "OJK", "BI", "Bank Indonesia", "LPS", "Polri", "TNI", "TNI AD",
    "TNI AL", "TNI AU", "DPR RI", "DPR", "DPD", "DPD RI", "MPR", "MPR RI",
    "BIN", "BNN", "BNPT", "BPOM", "BMKG", "BPS", "BNPB", "BPBD", "Basarnas",
    "Baznas", "KPU", "Bawaslu", "DKPP", "Komnas HAM", "Komnas Perempuan",
    "KPAI", "KPI", "Ombudsman", "BRIN", "BPKP", "BPK", "BPK RI",
    "Mahkamah Agung", "Mahkamah Konstitusi", "Komisi Yudisial", "LPSK",
    "LKPP", "Bakamla", "Bea Cukai", "Ditjen Pajak", "Ditjen Imigrasi",
    "Imigrasi", "Lemhannas", "BKN", "BKKBN", "Bapanas", "BSSN", "PPATK",
    "BPIP", "KSP", "Setneg", "Setkab", "Wantimpres", "Kejagung", "KPK RI",
    "Bapenda", "Bappeda", "Bappenas", "BPN", "ATR/BPN", "Kemenko Polhukam",
    "Kemenko Perekonomian", "Kemenko PMK", "Kemenko Marves",
    # --- ministries (abbreviations) ---
    "Kemendagri", "Kemenlu", "Kemenhan", "Kemenag", "Kemenkumham",
    "Kemenkeu", "Kemenperin", "Kemendag", "Kementan", "Kemenhub",
    "Kemenkes", "Kemensos", "Kemenpar", "Kemenpora", "Kemenaker",
    "Kemendikbud", "Kemendikbudristek", "Kemendikdasmen", "Kemendiktisaintek",
    "Kemenparekraf", "KemenPPPA", "KemenPANRB", "Kemenkop", "KemenKopUKM",
    "Kemendes", "Kemenimipas", "KemenPU", "Kementerian BUMN", "Kominfo",
    "Komdigi", "KLHK", "KKP", "ESDM", "PUPR",
    # --- state-owned enterprises (BUMN) ---
    "PLN", "Pertamina", "Pertamina Patra Niaga", "Telkom", "Telkomsel",
    "Bulog", "Antam", "Bukit Asam", "PT Timah", "MIND ID", "Inalum",
    "Pupuk Indonesia", "Pusri", "Petrokimia Gresik", "Semen Indonesia",
    "Krakatau Steel", "Perhutani", "PTPN", "Jasa Marga", "Hutama Karya",
    "Waskita Karya", "Wijaya Karya", "WIKA", "Adhi Karya",
    "Brantas Abipraya", "Jasa Raharja", "Taspen", "Asabri",
    "Bank Mandiri", "BRI", "BNI", "BTN", "BSI", "Bank Syariah Indonesia",
    "Pegadaian", "PNM", "Bio Farma", "Kimia Farma", "Indofarma", "Pindad",
    "PT PAL", "Dirgantara Indonesia", "INKA", "KAI", "Kereta Api Indonesia",
    "KCIC", "Damri", "Pelni", "Pelindo", "ASDP", "Angkasa Pura",
    "InJourney", "Garuda Indonesia", "Citilink", "Pos Indonesia",
    "Perumnas", "Peruri", "Danantara", "BPJS Kesehatan",
    "BPJS Ketenagakerjaan",
    # --- major private companies & banks ---
    "BCA", "CIMB Niaga", "Bank Danamon", "Bank Permata", "Maybank",
    "Bank Panin", "OCBC", "BTPN", "Bank Mega", "Astra International",
    "Astra Honda Motor", "Daihatsu", "Indofood", "Wings", "Mayora",
    "Unilever Indonesia", "Djarum", "Gudang Garam", "Sampoerna",
    "Sido Muncul", "Kalbe Farma", "Alfamart", "Indomaret", "Lippo Group",
    "Ciputra", "Sinar Mas", "Summarecon", "Agung Sedayu", "GoTo", "Gojek",
    "Tokopedia", "Shopee", "Lazada", "Bukalapak", "Blibli", "Grab",
    "Traveloka", "OVO", "DANA", "Indosat", "Indosat Ooredoo Hutchison",
    "XL Axiata", "Smartfren", "Biznet", "First Media", "MNC Group",
    "Emtek", "Freeport Indonesia", "Freeport", "Vale Indonesia", "Adaro",
    "Medco", "Chandra Asri",
    # --- political parties ---
    "PDI Perjuangan", "PDIP", "Golkar", "Partai Golkar", "Gerindra",
    "PKB", "PKS", "NasDem", "Partai NasDem", "PAN", "Partai Demokrat",
    "PPP", "PSI", "Perindo", "Hanura", "PBB", "Partai Gelora", "PKN",
    "Partai Buruh", "Partai Ummat",
    # --- mass / religious organizations ---
    "NU", "Nahdlatul Ulama", "PBNU", "Muhammadiyah", "MUI", "GP Ansor",
    "Ansor", "Banser", "Fatayat NU", "Muslimat NU", "Aisyiyah",
    "Pemuda Muhammadiyah", "HMI", "PMII", "GMNI", "IMM", "KAMMI", "KNPI",
    "Pemuda Pancasila", "Persis", "Al Washliyah", "PGI", "KWI", "Walubi",
    "PHDI", "Matakin", "Dewan Masjid Indonesia",
    # --- professional & industry associations ---
    "Kadin", "Apindo", "Hipmi", "IWAPI", "HKTI", "KSPI", "KSPSI",
    "Aprindo", "Gaikindo", "AISI", "APJII", "Perbanas", "Asbanda", "AAJI",
    "REI", "Organda", "ASITA", "PHRI", "IDI", "IDAI", "PDGI", "IBI",
    "PPNI", "IAI", "Peradi", "PGRI", "PWI", "AJI", "IJTI", "Dewan Pers",
    # --- sports ---
    "PSSI", "PBSI", "PBVSI", "Perbasi", "PRSI", "PASI", "KONI", "KOI",
    "NOC Indonesia", "Persija", "Persib", "Persebaya", "Arema FC", "Arema",
    "PSM Makassar", "PSIS", "PSS Sleman", "Bali United", "Borneo FC",
    "Persita", "Persik", "Dewa United", "Madura United", "Persipura",
    # --- humanitarian & international ---
    "PMI", "Palang Merah Indonesia", "ICRC", "Interpol", "PBB", "UNESCO",
    "UNICEF", "UNHCR", "WHO", "ILO", "FAO", "IMF", "Bank Dunia",
    "World Bank", "ADB", "IsDB", "ASEAN", "APEC", "G20", "OKI", "FIFA",
    "AFC", "AFF", "IOC", "Uni Eropa", "European Union", "NATO",
    "Amnesty International", "Greenpeace", "WWF", "Walhi", "ICW", "YLKI",
    "YLBHI", "LBH", "Kontras",
]


def _build_known_re():
    return re.compile(
        r"\b(" + "|".join(re.escape(k) for k in
                          sorted(set(_KNOWN), key=len, reverse=True)) + r")\b")


_KNOWN_RE = _build_known_re()
_EXTERNAL_LOADED = False
DICTIONARY_FILENAMES = ("organization_dictionary.txt", "kamus_organisasi.txt")


def load_org_dictionary(path="", log=print):
    """Merge the user's own organization list (one name per line) into the
    built-in dictionary. Empty path -> auto-detect the default filenames in
    the working folder. Returns the number of new names."""
    global _KNOWN_RE, _EXTERNAL_LOADED
    _EXTERNAL_LOADED = True
    import os
    if not path:
        path = next((f for f in DICTIONARY_FILENAMES if os.path.exists(f)), "")
    if not path or not os.path.exists(path):
        return 0
    new = 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            name = line.strip().strip(",;")
            if len(name) >= 2 and not name.startswith("#") and name not in _KNOWN:
                _KNOWN.append(name)
                new += 1
    if new:
        _KNOWN_RE = _build_known_re()
        log(f"🏛️ Organization dictionary '{path}': {new} names added "
            f"(total {len(set(_KNOWN))}).")
    return new

# Acronym in parentheses right after a name: (NOC Indonesia), (KPK)
_PAREN_RE = re.compile(r"\(([A-Z][A-Za-z .-]{1,38})\)")

_TRAIL_CONNECT = ("dan", "of", "the", "untuk")
MAX_ORGS = 15


def _clean(name):
    parts = name.split()
    while parts and parts[-1].lower() in _TRAIL_CONNECT:
        parts.pop()
    return " ".join(parts).strip(" ,.;:-")


def _split_conjunction(name):
    """'Bank Indonesia dan OJK' -> ['Bank Indonesia', 'OJK'] — but
    'Kementerian Energi dan Sumber Daya Mineral' stays whole: only split
    when a side is independently a known org or starts with a prefix."""
    if " dan " in name:
        left, right = name.split(" dan ", 1)
        left, right = _clean(left), _clean(right)
        if (_KNOWN_RE.fullmatch(right) or _PREFIX_RE.match(right)
                or _KNOWN_RE.fullmatch(left)):
            return [left] + _split_conjunction(right)
    return [name]


def extract_orgs(text):
    """Return up to MAX_ORGS organization names, in order of appearance."""
    if not text:
        return []
    if not _EXTERNAL_LOADED:      # pick up the user's own list, once
        try:
            load_org_dictionary(log=lambda *a: None)
        except Exception:
            pass
    found = []          # (position, name)
    for m in _PREFIX_RE.finditer(text):
        for name in _split_conjunction(_clean(m.group(1) + m.group(2))):
            if len(name.split()) >= 2 or _KNOWN_RE.fullmatch(name):
                found.append((m.start(), name, m.end()))
    for m in _COMPANY_RE.finditer(text):
        name = _clean(m.group(1) + " " + m.group(2))
        if len(name.split()) >= 2:
            found.append((m.start(), name, m.end()))
    for m in _KNOWN_RE.finditer(text):
        found.append((m.start(), m.group(1), m.end()))
    # acronyms in parentheses directly after an accepted org
    for pos, name, end in list(found):
        m = _PAREN_RE.match(text[end:end + 42].lstrip()) or \
            _PAREN_RE.match(text[end:end + 42])
        if m:
            inner = _clean(m.group(1))
            if inner and not inner.islower():
                found.append((end, inner, end))
    found.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _, name, _ in found:
        key = name.lower()
        # drop names fully contained in an already-accepted longer name
        if key in seen or any(key in s and key != s for s in seen):
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_ORGS:
            break
    return out
