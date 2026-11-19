"""common/protocol.py için birim testleri.

Çalıştırma (depo kökünden):
    python -m unittest tests.test_protocol -v
"""

import io
import unittest

from common.protocol import (
    CMD_GET, CMD_SET, CMD_STATS, ERR_KEY, ERR_UNKNOWN, ERR_VALUE,
    MAX_KEY_LEN, MAX_LINE_LEN, MAX_VALUE_LEN,
    LineTooLongError, encode_line, format_stats, format_value,
    parse_line, read_line, validate_key, validate_value,
    # Level 2
    CMD_REPL, CMD_SYNC, ERR_NOT_PRIMARY, MAX_PORT, RESP_END,
    _parse_positive_int, format_entry, format_not_primary, format_repl, format_sync,
    parse_entry, parse_not_primary,
)


class TestValidateKey(unittest.TestCase):
    def test_gecerli(self):
        self.assertIsNone(validate_key("k1"))
        self.assertIsNone(validate_key("a" * MAX_KEY_LEN))   # tam 16
        self.assertIsNone(validate_key("şehirçğü"))           # Türkçe karakter

    def test_gecersiz(self):
        self.assertEqual(validate_key(None), ERR_KEY)
        self.assertEqual(validate_key(""), ERR_KEY)
        self.assertEqual(validate_key("a" * (MAX_KEY_LEN + 1)), ERR_KEY)  # 17
        for k in ["a b", "a\tb", "a\rb", "a\nb"]:
            self.assertEqual(validate_key(k), ERR_KEY, repr(k))


class TestValidateValue(unittest.TestCase):
    def test_gecerli(self):
        self.assertIsNone(validate_value("x"))
        self.assertIsNone(validate_value("x" * MAX_VALUE_LEN))  # tam 4080
        self.assertIsNone(validate_value("merhaba dünya "))      # boşluk serbest

    def test_gecersiz(self):
        self.assertEqual(validate_value(None), ERR_VALUE)
        self.assertEqual(validate_value(""), ERR_VALUE)
        self.assertEqual(validate_value("x" * (MAX_VALUE_LEN + 1)), ERR_VALUE)  # 4081
        self.assertEqual(validate_value("a\nb"), ERR_VALUE)


class TestParseLine(unittest.TestCase):
    def test_set_basit(self):
        r = parse_line("SET k1 merhaba\n")
        self.assertTrue(r.ok)
        self.assertEqual((r.cmd, r.key, r.value), (CMD_SET, "k1", "merhaba"))

    def test_set_bosluklu_value(self):
        r = parse_line("SET k1 merhaba güzel dünya")
        self.assertEqual(r.value, "merhaba güzel dünya")

    def test_value_bosluklari_korunur(self):
        # Ayırıcıdan sonraki ek boşluk ve sondaki boşluk value'ya aittir
        r = parse_line("SET k1  iki boşluk \r\n")
        self.assertEqual(r.value, " iki boşluk ")

    def test_crlf_atilir(self):
        r = parse_line("GET k1\r\n")
        self.assertEqual((r.cmd, r.key), (CMD_GET, "k1"))

    def test_kucuk_harf_komut(self):
        r = parse_line("set k1 v")
        self.assertEqual(r.cmd, CMD_SET)
        r = parse_line("stats")
        self.assertEqual(r.cmd, CMD_STATS)

    def test_key_buyuk_kucuk_harf_korunur(self):
        self.assertEqual(parse_line("GET Key1").key, "Key1")

    def test_get(self):
        r = parse_line("GET k1")
        self.assertEqual((r.cmd, r.key, r.value), (CMD_GET, "k1", None))

    def test_stats(self):
        r = parse_line("STATS")
        self.assertTrue(r.ok)
        self.assertEqual(r.cmd, CMD_STATS)

    def test_hatalar(self):
        # docs/protokol.md, bölüm 3 "Eksik ve fazla argümanlar" + örnek oturum
        durumlar = {
            "": ERR_UNKNOWN,
            "\r\n": ERR_UNKNOWN,
            "HELLO": ERR_UNKNOWN,
            "STATS fazla": ERR_UNKNOWN,
            "GET": ERR_KEY,
            "GET ": ERR_KEY,
            "GET k1 fazla": ERR_KEY,
            "SET": ERR_KEY,
            "SET k1": ERR_VALUE,
            "SET k1 ": ERR_VALUE,                       # boş value
            "SET  k1 v": ERR_KEY,                       # çift boşluk -> boş key
            "SET bu_key_17_karakte x": ERR_KEY,         # 17 karakter (örnek oturumdaki)
            "SET " + "a" * 17: ERR_KEY,                 # value yok ama önce key kontrol edilir
            "SET k1 " + "x" * (MAX_VALUE_LEN + 1): ERR_VALUE,
        }
        for satir, beklenen in durumlar.items():
            with self.subTest(satir=satir[:40]):
                r = parse_line(satir)
                self.assertFalse(r.ok)
                self.assertEqual(r.error, beklenen)

    def test_sinirda_gecerli(self):
        r = parse_line("SET " + "a" * MAX_KEY_LEN + " " + "x" * MAX_VALUE_LEN)
        self.assertTrue(r.ok)



class TestReadLine(unittest.TestCase):
    @staticmethod
    def akis(metin: str) -> io.StringIO:
        # Soket makefile(newline="\n") ile aynı davranış: \r çevrilmez
        return io.StringIO(metin, newline="\n")

    def test_birden_fazla_satir(self):
        s = self.akis("SET a 1\nGET a\r\n")
        self.assertEqual(read_line(s), "SET a 1")
        self.assertEqual(read_line(s), "GET a")
        self.assertIsNone(read_line(s))   # EOF

    def test_son_satir_newline_olmadan(self):
        self.assertEqual(read_line(self.akis("STATS")), "STATS")

    def test_sinirda(self):
        satir = "x" * MAX_LINE_LEN
        self.assertEqual(read_line(self.akis(satir + "\n")), satir)
        self.assertEqual(read_line(self.akis(satir + "\r\n")), satir)

    def test_cok_uzun(self):
        with self.assertRaises(LineTooLongError):
            read_line(self.akis("x" * (MAX_LINE_LEN + 1) + "\n"))
        with self.assertRaises(LineTooLongError):
            read_line(self.akis("x" * (MAX_LINE_LEN + 50) + "\n"))


class TestFormat(unittest.TestCase):
    def test_encode(self):
        self.assertEqual(encode_line("VALUE çğ"), "VALUE çğ\n".encode("utf-8"))

    def test_format(self):
        self.assertEqual(format_value("a b"), "VALUE a b")
        self.assertEqual(format_stats(3), "STATS 3")


# ---------------------------------------------------------------------------
# Level 2 (protokol sürümü 1.1)
# Kararlar: seq = yalnızca ASCII rakam, >= 1; port = aynı kural, <= 65535;
#           REPL kontrol sırası = komut, seq, key, value; argümansız "SYNC" -> ERR UNKNOWN
# ---------------------------------------------------------------------------

class TestParsePositiveInt(unittest.TestCase):
    """seq ve port'un ortak ayrıştırıcısı. Özel (_) bir fonksiyonu doğrudan test ediyoruz,
    çünkü tüm katı-sayı kuralları burada toplanıyor: tek yerde, tek test sınıfı."""

    def test_valid(self):
        self.assertEqual(_parse_positive_int("1"), 1)
        self.assertEqual(_parse_positive_int("42"), 42)
        self.assertEqual(_parse_positive_int("007"), 7)                      # baştaki sıfır: bilinçli hoşgörü
        self.assertEqual(_parse_positive_int(str(2**63 - 1)), 2**63 - 1)    # çok büyük seq sorun değil

    def test_invalid(self):
        # int() bunların hepsini kabul eder (ya da isdigit() True döner); bizim kuralımız reddeder
        for text in ["", "0", "-1", "+7", " 7", "7 ", "1_000", "abc", "7.0", "٧", "١٢", "²"]:
            with self.subTest(text=text):
                self.assertIsNone(_parse_positive_int(text))

    def test_upper_bound(self):
        self.assertEqual(_parse_positive_int("65535", upper=MAX_PORT), 65535)
        self.assertIsNone(_parse_positive_int("65536", upper=MAX_PORT))


class TestParseLineRepl(unittest.TestCase):
    def test_valid_with_spaced_value(self):
        r = parse_line("REPL 7 k1 merhaba güzel dünya")
        self.assertTrue(r.ok)
        self.assertEqual((r.cmd, r.seq, r.key, r.value), (CMD_REPL, 7, "k1", "merhaba güzel dünya"))
        self.assertIsNone(r.port)

    def test_value_spaces_preserved_and_crlf_removed(self):
        r = parse_line("REPL 7 k1  iki boşluk \r\n")
        self.assertEqual(r.value, " iki boşluk ")

    def test_lowercase_command(self):
        self.assertEqual(parse_line("repl 7 k1 v").cmd, CMD_REPL)

    def test_valid_at_limits(self):
        r = parse_line(format_repl(2**63 - 1, "a" * MAX_KEY_LEN, "x" * MAX_VALUE_LEN))
        self.assertTrue(r.ok)
        self.assertEqual(len(r.key), MAX_KEY_LEN)
        self.assertEqual(len(r.value), MAX_VALUE_LEN)

    def test_invalid_seq(self):
        for line in ["REPL 0 k1 v", "REPL -1 k1 v", "REPL abc k1 v", "REPL +7 k1 v",
                     "REPL 1_000 k1 v", "REPL ٧ k1 v", "REPL  k1 v"]:   # son: boş seq
            with self.subTest(line=line):
                r = parse_line(line)
                self.assertFalse(r.ok)
                self.assertEqual(r.error, ERR_UNKNOWN)

    def test_missing_fields(self):
        durumlar = {
            "REPL": ERR_UNKNOWN,         # seq yok
            "REPL 7": ERR_KEY,           # key yok
            "REPL 7 k1": ERR_VALUE,      # value yok
            "REPL 7 k1 ": ERR_VALUE,     # boş value
        }
        for line, beklenen in durumlar.items():
            with self.subTest(line=line):
                self.assertEqual(parse_line(line).error, beklenen)

    def test_bad_key(self):
        self.assertEqual(parse_line("REPL 7 " + "a" * (MAX_KEY_LEN + 1) + " v").error, ERR_KEY)

    def test_bad_value(self):
        self.assertEqual(parse_line("REPL 7 k1 " + "x" * (MAX_VALUE_LEN + 1)).error, ERR_VALUE)

    def test_check_order_is_seq_then_key_then_value(self):
        long_key = "a" * (MAX_KEY_LEN + 1)
        self.assertEqual(parse_line("REPL abc " + long_key + " ").error, ERR_UNKNOWN)   # seq key'den önce
        self.assertEqual(parse_line("REPL 7 " + long_key).error, ERR_KEY)               # key value'dan önce


class TestParseLineSync(unittest.TestCase):
    def test_valid(self):
        r = parse_line("SYNC 5001")
        self.assertTrue(r.ok)
        self.assertEqual((r.cmd, r.port), (CMD_SYNC, 5001))
        self.assertIsNone(r.seq)

    def test_lowercase_and_port_limits(self):
        self.assertEqual(parse_line("sync 1").port, 1)
        self.assertEqual(parse_line("SYNC 65535").port, 65535)

    def test_invalid_port(self):
        for line in ["SYNC 0", "SYNC 65536", "SYNC abc", "SYNC -1", "SYNC ", "SYNC ٧"]:
            with self.subTest(line=line):
                self.assertEqual(parse_line(line).error, ERR_UNKNOWN)

    def test_bare_sync_and_extra_arguments(self):
        # Sürüm 1.0'da "SYNC" geçerliydi; 1.1'de port şart (kasıtlı kırılma, protokol.md'de yazılı)
        for line in ["SYNC", "SYNC 5001 fazla", "SYNC 5001 "]:
            with self.subTest(line=line):
                self.assertEqual(parse_line(line).error, ERR_UNKNOWN)


class TestLineLength(unittest.TestCase):
    def test_longest_repl_and_entry_fit_in_max_line_len(self):
        # En kötü durum: 19 haneli seq (2**63 - 1), 16 karakterlik key, 4080 karakterlik value
        seq, key, value = 2**63 - 1, "k" * MAX_KEY_LEN, "v" * MAX_VALUE_LEN
        repl = format_repl(seq, key, value)
        entry = format_entry(seq, key, value)
        self.assertEqual(len(repl), 5 + 19 + 1 + 16 + 1 + 4080)     # "REPL " + seq + " " + key + " " + value
        self.assertEqual(len(entry), 6 + 19 + 1 + 16 + 1 + 4080)    # "ENTRY " biraz daha uzun
        self.assertLessEqual(len(repl), MAX_LINE_LEN)
        self.assertLessEqual(len(entry), MAX_LINE_LEN)
        self.assertTrue(parse_line(repl).ok)                        # ve sunucu bu satırı gerçekten kabul eder
        self.assertIsNotNone(parse_entry(entry))


class TestRoundTrips(unittest.TestCase):
    def test_format_strings_match_the_spec(self):
        self.assertEqual(format_repl(7, "k1", "merhaba dünya"), "REPL 7 k1 merhaba dünya")
        self.assertEqual(format_sync(5001), "SYNC 5001")
        self.assertEqual(format_entry(7, "k1", "merhaba dünya"), "ENTRY 7 k1 merhaba dünya")
        self.assertEqual(format_not_primary(5000), "ERR NOT_PRIMARY 5000")

    def test_repl_round_trip(self):
        for value in ["merhaba", "merhaba güzel dünya", " başta ve sonda boşluk ", "şğüçöı İ"]:
            with self.subTest(value=value):
                r = parse_line(format_repl(7, "k1", value))
                self.assertEqual((r.seq, r.key, r.value), (7, "k1", value))

    def test_entry_round_trip(self):
        for value in ["merhaba", "iki  boşluk", " başta ve sonda boşluk ", "şğüçöı İ"]:
            with self.subTest(value=value):
                self.assertEqual(parse_entry(format_entry(9, "k2", value)), (9, "k2", value))

    def test_sync_and_not_primary_round_trip(self):
        self.assertEqual(parse_line(format_sync(5001)).port, 5001)
        self.assertEqual(parse_not_primary(format_not_primary(5000)), 5000)


class TestParseEntry(unittest.TestCase):
    def test_valid_line(self):
        self.assertEqual(parse_entry("ENTRY 7 k1 merhaba dünya"), (7, "k1", "merhaba dünya"))

    def test_end_returns_none(self):
        # parse_entry "END"i bozuk satırdan ayıramaz: çağıran önce RESP_END'i kontrol eder
        self.assertEqual(RESP_END, "END")
        self.assertIsNone(parse_entry("END"))

    def test_broken_lines_return_none(self):
        bozuklar = ["", "ENTRY", "ENTRY 7 k1", "ENTRY 7 k1 ", "ENTRY x k1 v", "ENTRY 0 k1 v",
                    "REPL 7 k1 v", "ENTRY 7 " + "a" * (MAX_KEY_LEN + 1) + " v",
                    "ENTRY 7 k1 " + "x" * (MAX_VALUE_LEN + 1)]
        for line in bozuklar:
            with self.subTest(line=line[:40]):
                self.assertIsNone(parse_entry(line))


class TestParseNotPrimary(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_not_primary("ERR NOT_PRIMARY 5000"), 5000)
        self.assertEqual(parse_not_primary(ERR_NOT_PRIMARY + " 65535"), 65535)

    def test_other_responses_return_none(self):
        for response in ["OK", "ERR KEY", "VALUE x", "", "ERR NOT_PRIMARY", "ERR NOT_PRIMARY ",
                         "ERR NOT_PRIMARY5000"]:
            with self.subTest(response=response):
                self.assertIsNone(parse_not_primary(response))

    def test_bad_port(self):
        for response in ["ERR NOT_PRIMARY abc", "ERR NOT_PRIMARY 0", "ERR NOT_PRIMARY 70000",
                         "ERR NOT_PRIMARY -5", "ERR NOT_PRIMARY 5000 fazla"]:
            with self.subTest(response=response):
                self.assertIsNone(parse_not_primary(response))


if __name__ == "__main__":
    unittest.main()
