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


if __name__ == "__main__":
    unittest.main()
