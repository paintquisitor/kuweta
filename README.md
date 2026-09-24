# Kuweta — Kefir i Kalinka

Lokalna aplikacja do monitorowania dwóch kuwet z podglądem kamery RTSP. Nowe nagrania są pobierane i analizowane przez lokalnego Qwena. Wyniki próbne trafiają do historii. Symulacje są osobno oznaczone i domyślnie ukryte.

## Uruchomienie

Wymagany Python 3.10 lub nowszy. Analiza wymaga FFmpeg/ffprobe, środowiska Tapo opisanego poniżej i działającego serwera Qwen z obsługą obrazów.

```sh
python3 server.py
```

Otwórz [http://127.0.0.1:8765](http://127.0.0.1:8765). Na Windows użyj `py server.py`. Zatrzymanie: Ctrl+C. Inny port: `python3 server.py --port 8766`.

Domyślnie serwer nasłuchuje również w sieci lokalnej. Na telefonie podłączonym do tej samej sieci Wi-Fi otwórz `http://ADRES_IP_KOMPUTERA:8765` (adres IP znajdziesz w ustawieniach sieci komputera). Adres `127.0.0.1` na telefonie wskazuje sam telefon, nie komputer. Komputer musi pozostać włączony i nieuśpiony.

Aby ograniczyć dostęp do samego komputera, uruchom `python3 server.py --host 127.0.0.1`. Aplikacja nie zawiera jeszcze logowania; nie wystawiaj portu do Internetu.

## Co działa

- Podsumowania Kefira i Kalinki oraz osobne mapy dwóch kuwet.
- Symulacja wizyty trwającej około 8 sekund: mocz, kał, oba naraz, brak widocznych oznak lub wynik niepewny.
- Osobne miejsca moczu i kału na mapie (M — owal, K — prostokąt), filtry historii, ręczne potwierdzenie i eksport CSV. Sam kał nie zwiększa licznika moczu. Istniejąca baza jest aktualizowana bez usuwania wizyt.
- Wybór kota, kuwety i jednego z dziewięciu rejonów. Oddzielne wizyty mogą trwać jednocześnie w obu kuwetach; jeden kot nie może jednocześnie korzystać z obu.
- Godzina wejścia, wyjścia i czas wizyty; aktualizacje przez SSE bez odświeżania strony.
- Historia, filtrowanie, szczegóły, ręczna ocena wyniku/miejsca i notatka z zachowaniem audytu.
- Sprzątanie usuwa plamy z bieżącej mapy, zachowując historię.
- Powiadomienia w panelu; opcjonalne powiadomienia przeglądarkowe po udzieleniu zgody. Karta aplikacji musi pozostać otwarta. To nie jest jeszcze push na zamknięty telefon.
- Eksport CSV, zapis w SQLite oraz oznaczenie przerwanej wizyty jako niepewnej po restarcie.
- Czytelny stan braku połączenia i automatyczne ponawianie połączenia.

Przy pierwszym uruchomieniu powstają cztery przykładowe wizyty. Dane przechowywane są w `data/kuweta.sqlite3`, poza repozytorium. Nie są wysyłane do Internetu. Aby użyć oddzielnej bazy, uruchom `python3 server.py --db data/inny-test.sqlite3`.

## Podgląd kamery

Wymagany FFmpeg dostępny w PATH. W `.env` uzupełnij `CAMERA_HOST`, `CAMERA_USERNAME` i `CAMERA_PASSWORD` danymi osobnego konta kamery utworzonego w aplikacji Tapo. Nie jest to konto chmurowe TP-Link. Dane pozostają na serwerze. Po odrzuceniu hasła aplikacja czeka na zmianę danych w `.env`, bez ponawiania błędnego logowania.

Podgląd korzysta z RTSP `/stream1`, pokazuje jedną klatkę na sekundę o szerokości 1280 px i nie zapisuje zdjęć. Wszystkie przeglądarki korzystają z jednego połączenia z kamerą. Podgląd nie uruchamia analizy Qwen ani rejestracji rzeczywistych wizyt.

Pod podglądem można zaznaczyć cztery narożniki każdej kuwety i zapisać obszary w bazie. Kuweta 01 to czarna, 02 — jasna. Współrzędne są względne wobec obrazu; po obrocie kamery lub przesunięciu kuwet zaznacz je ponownie. Zapis obszarów nie uruchamia detekcji.

## Wasz Qwen

Nie instalujemy ani nie zastępujemy działającego modelu. Plik `qwen.py` zawiera adapter do serwera z API zgodnym z OpenAI (`/v1/chat/completions`) i obsługą obrazów.

Skopiuj `.env.example` do `.env`, wpisz adres Waszego serwera i jego identyfikator modelu, a następnie uruchom aplikację ponownie. Jeśli Qwen działa na innym komputerze, `127.0.0.1` trzeba zastąpić jego adresem. Klucze pozostają po stronie serwera. Panel pokazuje wyłącznie, czy konfiguracja istnieje — nie traktuje tego jako testu połączenia.

Symulator i podgląd nie wywołują Qwena. Kolejka nagrań wywołuje adapter `analyze_images` i waliduje odpowiedzi JSON. Odpowiedź modelu to obserwacje; nie nadaje automatycznie statusu „mocz potwierdzony”.

## Kontrola działania

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Node jest potrzebny tylko do opcjonalnej kontroli składni JavaScript. Testy używają osobnej tymczasowej bazy i nie zmieniają historii w aplikacji.

## Automatyczna analiza nagrań

Archiwum kamery jest sprawdzane w tle co około 60 sekund, kiedy serwer aplikacji działa (strona nie musi być otwarta). Przygotuj środowisko: `python3 -m venv .venv-tapo` i `.venv-tapo/bin/pip install -r requirements-tapo.txt`. W `.env` potrzebne są `CAMERA_HOST` i `TAPO_CLOUD_PASSWORD`; w Tapo włącz zgodność z aplikacjami innych firm. Po błędzie odczytu ponowienie następuje po 5 minutach. Hasło pozostaje lokalnie.

Panel „Nagrania z kamery” zapisuje w SQLite początek i koniec filmu w UTC, pokazując czas Europe/Warsaw. Identyfikator kamery i początek nagrania zapobiegają duplikatom, zmiana końca aktualizuje istniejący wpis. Po restarcie odczyt wraca do ostatniego poprawnego sprawdzenia z zapasem jednego dnia; przy pierwszym uruchomieniu sprawdzane są ostatnie trzy dni kalendarzowe. Film uznawany jest za zakończony najwcześniej minutę po zgłoszonym końcu. Dwa ręcznie oznaczone testy z człowiekiem są pomijane. Nowe nagrania trafiają do trwałej kolejki: pobranie, weryfikacja długości filmu, analiza obecności co 2 sekundy i porównanie wycinków żwirku. Film oraz wynik są dostępne w panelu. Błąd powoduje ponowienie po 5 minutach, maksymalnie 3 próby; restart odzyskuje przerwane zadania. Filmy dłuższe niż 10 minut wymagają ręcznej oceny. Pliki pozostają w `data/recordings/processed` bez automatycznego usuwania.

Czas obserwacji to czas początku filmu plus pozycja klatki; nie jest dokładnym czasem wejścia/wyjścia. Wizyta wymaga minimum dwóch kolejnych klatek z kotem wewnątrz kuwety. Brak kota w próbkach nie wyklucza krótkiej wizyty. Ucięty materiał lub niewidoczny żwirek daje wynik niepewny. Powiązanie wizyt między osobnymi filmami nie jest jeszcze obsługiwane.

Tożsamość kota pozostaje nieznana do ręcznego przypisania w szczegółach wizyty; automatyczna identyfikacja wymaga zdjęć wzorcowych i walidacji. Mapa wskazuje przybliżone pola 3×3, górny rząd odpowiada górze wycinka kamery. Kalibracja kuwet jest statyczna — po przesunięciu sprawdź narożniki. Skuteczność rozpoznawania moczu i kału wymaga sprawdzenia na rzeczywistych wizytach.

Pełny kontekst: [plan implementacji](PLAN_IMPLEMENTACJI.md).
