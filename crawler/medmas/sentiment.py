"""Sentiment analysis for Indonesian news.

Two engines:
- "lexicon"     (default) — instant, no extra installs: curated Indonesian
                positive/negative word lists with negation handling
                ("tidak aman" counts as negative, "tidak rusak" as positive).
- "transformer" — optional upgrade, higher accuracy: HuggingFace model
                w11wo/indonesian-roberta-base-sentiment-classifier
                (requires: pip install transformers torch).

Both return lowercase labels: positive / negative / neutral.
"""

import re

POSITIVE = frozenset("""
bagus baik hebat sukses berhasil unggul juara prestasi penghargaan apresiasi
bangga optimis positif maju berkembang tumbuh meningkat naik surplus untung
laba menguntungkan efisien efektif inovatif kreatif solutif transparan
akuntabel amanah jujur adil aman nyaman damai tenteram sejahtera makmur
sehat pulih membaik stabil lancar mudah cepat tepat akurat berkualitas
unggulan terbaik favorit populer diminati laris ramah bersih indah asri
hijau lestari berkelanjutan produktif kompeten profesional handal tangguh
kuat solid kompak sinergi kolaborasi kerjasama dukungan mendukung membantu
bantuan hibah beasiswa gratis murah terjangkau hemat cair terealisasi
rampung tuntas selesai diresmikan diluncurkan dibuka melonjak melesat
memuaskan menggembirakan membanggakan menjanjikan bersyukur syukur senang
gembira bahagia antusias semangat apresiatif kondusif harmonis rukun
melimpah surplus rekor tercapai terpenuhi terwujud direalisasikan
diperbaiki dibenahi ditingkatkan dipermudah dipercepat digratiskan
menang kemenangan medali penghargaan juara peluang harapan solusi
""".split())

NEGATIVE = frozenset("""
buruk jelek gagal rugi kerugian bangkrut pailit defisit merosot anjlok
turun menurun melemah lesu krisis resesi inflasi mahal langka kelangkaan
korupsi korup suap gratifikasi pungli pungutan nepotisme kolusi mafia
penyelewengan penggelapan pencucian penipuan tipu curang kecurangan bohong
hoaks fitnah pencemaran manipulasi mark-up markup
kecelakaan tabrakan terguling terjatuh tenggelam tewas meninggal wafat
korban luka cedera kritis maut jenazah mayat
kebakaran terbakar hangus ledakan meledak bocor ambruk runtuh roboh longsor
banjir gempa tsunami erupsi bencana musibah kekeringan gagal-panen abrasi
rusak kerusakan hancur porak-poranda parah memprihatinkan kumuh tercemar
polusi limbah sampah bau
mencuri pencurian curanmor perampokan rampok begal maling penjarahan
pembunuhan membunuh penganiayaan aniaya pengeroyokan kekerasan pelecehan
pemerkosaan perundungan bullying penculikan penyanderaan ancaman teror
terorisme radikalisme narkoba sabu ganja miras tawuran bentrok kerusuhan
ricuh anarkis vandalisme pembacokan penusukan penembakan
ditangkap penangkapan ditahan penahanan tersangka terdakwa terpidana
dipenjara bui divonis dihukum didakwa dilaporkan digerebek razia buron
dpo diperiksa disita dipecat diberhentikan dicopot dinonaktifkan disanksi
diblokir dibekukan dicabut ilegal melanggar pelanggaran sengketa gugatan
digugat konflik polemik kontroversi protes demo demonstrasi unjuk mogok
boikot keluhan mengeluh keluh kecewa mengecewakan resah keresahan cemas
khawatir kekhawatiran takut ketakutan panik trauma stres depresi
marah kemarahan kesal geram murka menyesal prihatin duka berduka tragis
miris ironis sayangnya
macet kemacetan padam pemadaman gangguan terganggu terhambat tersendat
terlambat molor mangkrak terbengkalai tertunda batal dibatalkan ditunda
sulit kesulitan susah beban membebani memberatkan menunggak tunggakan
utang hutang miskin kemiskinan pengangguran menganggur phk dirumahkan
kelaparan gizi-buruk stunting wabah pandemi epidemi virus penyakit
terinfeksi positif-covid drop nge-drop ngedrop overload berbahaya bahaya
membahayakan darurat genting rawan kritis mencekam
""".split())

NEGATIONS = frozenset("tidak tak bukan belum tanpa kurang gagal anti "
                      "jangan tiada nihil minim".split())

_WORD_RE = re.compile(r"[a-z\-]+")


def lexicon_sentiment(text):
    """Score text with the lexicon. Return (label, score)."""
    if not text:
        return "neutral", 0
    words = _WORD_RE.findall(str(text).lower())
    score = 0
    for i, w in enumerate(words):
        val = 1 if w in POSITIVE else (-1 if w in NEGATIVE else 0)
        if not val:
            continue
        # negation within the two preceding words flips polarity
        if any(n in NEGATIONS for n in words[max(0, i - 2):i]):
            val = -val
        score += val
    if score >= 2:
        return "positive", score
    if score <= -2:
        return "negative", score
    return "neutral", score


def run_sentiment(db, engine, log=print):
    """Fill the sentiment column for articles that don't have one yet."""
    if engine in ("off", "", None, False):
        return
    queue = db.articles_without_sentiment()
    if not queue:
        return
    log(f"\n🧠 Sentiment analysis ({engine}): {len(queue)} articles...")

    if engine == "transformer":
        try:
            from transformers import pipeline as hf_pipeline
            import torch
            device = 0 if torch.cuda.is_available() else -1
            model = hf_pipeline(
                "sentiment-analysis",
                model="w11wo/indonesian-roberta-base-sentiment-classifier",
                device=device, truncation=True, max_length=512)
            queue = [(u, t, s) for u, t, s in queue if (s or t).strip()]
            for i in range(0, len(queue), 32):
                chunk = queue[i:i + 32]
                preds = model([(s or t)[:1000] for _, t, s in chunk])
                for (url, _, _), p in zip(chunk, preds):
                    db.set_sentiment(url, str(p["label"]).lower())
                log(f"   Sentiment: {min(i + 32, len(queue))}/{len(queue)}")
            log("✅ Sentiment done (transformer).")
            return
        except ImportError:
            log("⚠️ transformers not installed — falling back to lexicon engine. "
                "For the transformer engine run: pip install transformers torch")
        except Exception as e:
            log(f"⚠️ Transformer failed ({type(e).__name__}) — falling back to lexicon.")

    # lexicon engine (also the fallback)
    for url, title, summary in queue:
        label, _ = lexicon_sentiment(f"{title}. {summary}")
        db.set_sentiment(url, label)
    log(f"✅ Sentiment done (lexicon) for {len(queue)} articles.")
