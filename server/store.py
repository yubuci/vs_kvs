import threading
from common.protocol import MAX_KEYS, validate_key, validate_value, ERR_FULL

class Store:
    def __init__(self):
        self._data = {} 
        self._lock = threading.Lock()
        self._max_keys = MAX_KEYS

    def set(self, key, value): #key değerini ayarlar, None (başarılı) ya da hata kodu döndürür(err key,err value,err full)
        err = validate_key(key) or validate_value(value)
        if err:
            return err    # None (başarılı) ya da hata kodu döndürür
        
        with self._lock:
            # Sınır yalnızca YENİ key için geçerli; var olan key doluyken de güncellenir
            if key not in self._data and len(self._data) >= self._max_keys:
                return ERR_FULL
            self._data[key] = value
            return None
        
    def get(self, key):         # value ya da None
        with self._lock:
            return self._data.get(key)
        
    def count(self):             # kayıt sayısı
        with self._lock:
            return len(self._data)

    def snapshot(self):
        """Deponun o anki kopyasını döndürür (dosyaya kayıt ve ileride SYNC için).

        Kopya döndürülür ki çağıran taraf kilit olmadan güvenle gezebilsin.
        """
        with self._lock:
            return dict(self._data)