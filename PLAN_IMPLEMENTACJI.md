# Monitoring kuwety Kefira i Kalinki — plan implementacji

Stan: propozycja techniczna, 22.09.2026. Projekt nie został jeszcze zaimplementowany ani zweryfikowany na nagraniach kotów.

## 1. Rekomendacja i granice możliwości

Zbudować lokalny system dla dwóch otwartych kuwet: po jednej kamerze nad każdą kuwetą, mały model rozpoznający koty, wykrywanie pojawienia się ciemnej mokrej plamy, rejestrator wizyt i aplikacja web. Każda kamera powinna obejmować również wejście do swojej kuwety. Dodatkową kamerę przy wejściu dołożyć tylko wtedy, gdy identyfikacja lub widoczność podłoża z podstawowych ujęć okażą się niewystarczające. Waga jest opcjonalnym rozszerzeniem po pilotażu obrazu. Istniejący multimodalny Qwen może wspomagać analizę zakończonych wizyt. Nie trenować dużego modelu od zera.

Najłatwiejsze zadania to wykrywanie obecności, wejścia, wyjścia i czasu wizyty. Rozpoznawanie Kefira i Kalinki wymaga przykładów z docelowych kamer, również nocnych. Najtrudniejsze jest ustalenie, czy faktycznie oddano mocz, i gdzie znajduje się zakopana bryłka.

Pozycja kucająca, kopanie i zmiana wyglądu żwirku nie dowodzą oddania moczu. Dwie kamery ograniczają zasłonięcia, ale nie widzą przez kota ani żwirek. Waga mierzy zmianę masy, nie skład pozostawionego materiału. Wynikiem musi czasem być „nierozstrzygnięte”; większy model nie usuwa braku danych pomiarowych.

System wspiera obserwację i przygotowanie historii dla lekarza. Nie rozpoznaje choroby ani nie zastępuje badania. Próby oddania moczu z małą ilością lub brakiem moczu mogą oznaczać niedrożność wymagającą natychmiastowej pomocy weterynaryjnej — nie należy wtedy czekać na alarm aplikacji. [Cornell](https://www.vet.cornell.edu/departments-centers-and-institutes/cornell-feline-health-center/health-information/feline-health-topics/feline-lower-urinary-tract-disease)

## 2. Założenia do doprecyzowania

Potwierdzone przez opiekuna: dwie otwarte kuwety; Kalinka jest znacznie mniejsza, ma charakterystyczny ogon i sierść; Kefir jest dużym kotem; komputer ma RTX 4070 i 32 GB RAM. Według obserwacji opiekuna po oddaniu moczu na żwirku pojawia się ciemna plama. Zdjęcia kotów mogą zostać dostarczone na etapie zbierania danych.

Roboczo zakładamy stałe miejsce kamer, domową sieć Wi-Fi i komputer pracujący całodobowo. Do ustalenia: rodzaj żwirku, rozstaw kuwet, który kot choruje, system telefonu, system operacyjny PC, dokładny model Qwen oraz wariant karty i VRAM. Nie zakładamy zmiany kuwety lub żwirku tylko na potrzeby elektroniki.

Jeżeli koty mają inne kuwety lub załatwiają się poza polem widzenia, aplikacja informuje o braku zarejestrowanego zdarzenia, a nie o pewnym braku oddawania moczu.

## 3. Sprzęt

| Element | Propozycja | Zadanie / warunek |
|---|---|---|
| Kamera nr 1 | TP-Link Tapo C120 jako kandydat do pilotażu | Widok z góry, lekko ukośnie: podłoże i wejście do kuwety nr 1 |
| Kamera nr 2 | Druga C120, po sprawdzeniu ostrości i kadru | Analogiczny widok kuwety nr 2 |
| Dodatkowy widok wejścia | Opcjonalna kolejna kamera po próbach | Lepsze ujęcie sierści, ogona i sylwetki, jeśli podstawowy widok nie wystarcza |
| Mocowania | Sztywne uchwyty i zabezpieczenie przewodów | Stała geometria mapy, brak luźnych elementów nad kotem |
| Komputer | Istniejący PC: RTX 4070, 32 GB RAM | Rejestracja, aplikacja i małe modele; wyłączone usypianie |
| Dysk | SSD z limitem miejsca na nagrania | Pierwotnie 100–200 GB wolnego jako budżet przestrzeni, do korekty po pomiarze bitrate |
| Platforma wagowa | Opcjonalnie po jednej na kuwetę: belki tensometryczne, przetwornik HX711 lub odpowiednik, ESP32, sztywna podstawa | Pomiar masy przed i po wizycie oraz pomocniczo masy kota |
| Oświetlenie | Najpierw dostępne światło / IR kamery | Sprawdzić rozpoznawanie w nocy bez nagłych błysków |
| Zasilanie awaryjne | Opcjonalny UPS | Podtrzymanie komputera, sieci i kamer |

C120 ma 2560 × 1440, do 20 kl./s zależnie od jasności, H.264, Wi-Fi 2,4 GHz, zasilanie przewodowe, IP66 i tryby IR 850/940 nm. Nie ma Ethernetu. Dokumentacja TP-Link opisuje lokalny dostęp RTSP/ONVIF i utworzenie osobnego konta kamery. Wbudowane wykrywanie zwierząt nie rozpoznaje automatycznie Kefira i Kalinki. [Specyfikacja](https://www.tp-link.com/pl/home-networking/cloud-camera/tapo-c120/), [RTSP/ONVIF](https://www.tp-link.com/en/support/faq/2680/)

Najpierw kupić lub pożyczyć jedną kamerę i sprawdzić: ostrość na docelowej odległości, nocne rozpoznawanie, zasłonięcia, ciągły RTSP oraz działanie nagrywania po odcięciu dostępu kamery do Internetu. Pierwsza konfiguracja może wymagać aplikacji producenta. Drugi egzemplarz kupić po próbie. Jeśli potrzebne jest połączenie kablowe, wybrać kamerę PoE z RTSP po takim samym teście obrazu z bliska.

Kamera ma widzieć całą kuwetę i okolice wejścia; żwirek powinien zajmować dużą część kadru kamery górnej. Unikać ruchomego kadru, automatycznego śledzenia, syren i lamp błyskających przy wejściu kota. Kamerę i kable mocować poza zasięgiem pazurów.

Platforma musi uwzględnić masę kuwety, żwirku i najcięższego kota z zapasem. Cała kuweta opiera się wyłącznie na platformie. Pomiar stabilności i dryfu jest ważniejszy niż deklarowana rozdzielczość przetwornika. Roboczy cel: powtarzalność kilku gramów, do sprawdzenia pod rzeczywistym obciążeniem. Żwirek wyniesiony lub wyrzucony poza ważoną powierzchnię, kał, sprzątanie i dosypywanie zaburzają bilans. Nie przeliczać zmiany masy automatycznie na mililitry moczu.

Termowizję rozważyć wyłącznie po nieudanym pilotażu zwykłych kamer. Podczerwień do nocnego nagrywania nie jest termowizją; także kamera termiczna nie zobaczy moczu zasłoniętego kotem lub warstwą żwirku. Nie kupować jej jako gwarancji potwierdzania mikcji.

## 4. Modele AI

### Wykrywanie i tożsamość

- Gotowy mały detektor obiektów, np. YOLO z klasą kot, znajduje zwierzę; śledzenie łączy kolejne klatki w wizytę. Gotowe modele i obsługiwane zadania opisuje [Ultralytics](https://docs.ultralytics.com/models/).
- Osobny mały klasyfikator wycinka kota rozpoznaje Kefira lub Kalinkę. Przy słabym wyniku albo sprzecznych klatkach zwraca „nieznany”. Tożsamość ustalamy z kilku dobrych ujęć, głównie przy wejściu i wyjściu.
- Zacząć od dostrojenia gotowego modelu obrazowego. Orientacyjny zbiór pilotażowy: 200–500 zróżnicowanych wycinków na kota z wielu niezależnych wizyt, różnych dni, kierunków i trybów oświetlenia. To punkt startowy, nie gwarancja jakości.
- Dzielić dane na trening i test według całych wizyt/dni. Sąsiednie klatki tego samego nagrania nie mogą trafić do obu zbiorów.
- Masa kota jest dodatkową wskazówką, nie jedyną tożsamością. Przy podobnych kotach można rozważyć identyfikator na bezpiecznej obroży, jeśli koty ją tolerują; system nie wymaga go w pierwszej wersji.

### Zachowanie i Qwen

Na początku zapisywać pełne wizyty i oznaczać ręcznie: kopanie, kucanie, zakopywanie, samo wejście, możliwy mocz, kał, niewidoczne. Następnie porównać prostą analizę sekwencji z Qwenem. Dopiero jeśli wyniki uzasadniają dodatkowy trening, dostroić mały model zachowania. Gotowy model pozy człowieka nie dostarcza automatycznie prawidłowych punktów ciała kota; potrzebne byłyby zwierzęce dane i własne oznaczenia. [Dokumentacja pozy](https://docs.ultralytics.com/tasks/pose)

Qwen3.5-27B jest modelem multimodalnym obsługującym obrazy i wideo. Samo określenie „Qwen 27B” nie wystarcza do potwierdzenia możliwości istniejącej instalacji: trzeba sprawdzić pełną nazwę modelu oraz obsługę obrazu przez lokalny serwer. [Karta Qwen3.5-27B](https://huggingface.co/Qwen/Qwen3.5-27B)

Rola Qwena: analiza wybranych klatek lub krótkiego klipu po wizycie, opis widocznych zachowań, wskazanie niepewności. Nie analizować nim każdej klatki całodobowego strumienia. Używać odpowiedzi o ustalonej strukturze i wymagać wskazania fragmentów nagrania stanowiących podstawę wniosku. Deklarowana przez model pewność nie jest skalibrowanym prawdopodobieństwem.

Standardowa desktopowa RTX 4070 ma 12 GB VRAM; konkretny wariant należy sprawdzić na komputerze. Orientacyjnie same wagi 27 mld parametrów przy 4 bitach to około 13,5 GB, jeszcze bez pamięci roboczej. Taki model nie zmieści się w całości w 12 GB VRAM przy tej precyzji; częściowe wykonanie w RAM/CPU może pozwolić na uruchomienie kosztem opóźnienia. 32 GB RAM nie zastępuje VRAM. [Specyfikacja NVIDIA](https://www.nvidia.com/en-us/geforce/graphics-cards/40-series/rtx-4070-family/)

Na tym komputerze zacząć od małego detektora, klasyfikatora dwóch kotów i analizy plam. Do pomocniczej interpretacji przetestować kwantyzowany Qwen3.5-4B, który obsługuje obrazy; 27B pozostawić jako opcjonalny punkt porównania po wizycie. Nie kupować większej karty przed benchmarkiem. Benchmark obejmie pełne zakończone wizyty, zużycie pamięci i wpływ analizy na nieprzerwane nagrywanie. Awaria lub zajętość Qwena nie może blokować rejestracji. [Karta Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B)

## 5. Jak ustalać oddanie moczu

Przechowywać oddzielnie rodzaj zdarzenia i siłę dowodu. Wizyta może zawierać zarówno mocz, jak i kał.

### Główny sygnał: nowa ciemna plama

Obserwacja opiekuna pozwala nadać analizie plamy pierwszeństwo przed opcjonalną wagą. To hipoteza pomiarowa do sprawdzenia na docelowym żwirku, w dzień i w trybie nocnym.

1. Przed wejściem zapisać obraz odniesienia i maski już istniejących ciemnych miejsc.
2. W czasie całej wizyty analizować widoczne fragmenty podłoża, maskując kota i obszary zasłonięte. Zachować pierwsze klatki nowej plamy, zanim kot ją zakopie; samo zdjęcie po wyjściu nie wystarczy.
3. Wyrównać jasność obrazów i odrzucać zmiany całego kadru po zmianie ekspozycji lub przełączeniu IR. Podczas samego przełączenia wyniki mogą być nierozstrzygnięte.
4. Znaleźć nowy ciemniejszy obszar i śledzić jego utrzymywanie się, kształt oraz związek czasowy i przestrzenny z pozycją kota. Cień zwykle podąża za kotem, ale ta reguła sama nie rozstrzyga o moczu. Zagłębienia po kopaniu, kał i odsłonięte stare mokre miejsca są przykładami negatywnymi do oznaczenia.
5. Najpierw sprawdzić prostą analizę różnicy obrazu; jeśli niewystarczająca, dostroić mały model segmentacji mokrego/suchego żwirku na własnych ręcznie oznaczonych maskach. Nie zakładać, że ogólny model rozpoznaje mokry żwirek bez takich danych.
6. Powiązać maskę nowej plamy z bieżącą wizytą, kuwetą i rozpoznanym kotem. Jeśli w danym rejonie jest już plama, zmiana jej rozmiaru może być niejednoznaczna. Nie wymuszać nowego zdarzenia.
7. Po potwierdzeniu jakości na niezależnych wizytach dopuścić etykietę „wykryto nową mokrą plamę — prawdopodobny mocz”, z filmem. „Mocz potwierdzony” pozostaje oddzielnym poziomem dowodu.

| Komunikat | Znaczenie |
|---|---|
| Mocz potwierdzony | Opiekun potwierdził nową bryłkę/mokry obszar przypisany do tej wizyty lub istnieje jednoznaczny bezpośredni materiał dowodowy; automatyczne potwierdzanie wymaga odrębnej walidacji |
| Prawdopodobne oddanie moczu | Zgodne przesłanki z zachowania, podłoża i ewentualnie wagi, bez bezpośredniego potwierdzenia |
| Nie wykryto oznak oddania moczu | W nagraniu nie znaleziono takich oznak; nie jest to dowód, że moczu nie było |
| Nie można ocenić | Zasłonięcie, awaria, słaby obraz, zmieszane wizyty lub sprzeczne pomiary |

W pierwszej wersji automatyka nie nadaje etykiety „potwierdzone” wyłącznie na podstawie kucania, dodatniej różnicy masy lub odpowiedzi Qwena. Pozostawić przycisk potwierdzenia przez opiekuna i zapisać autora oraz podstawę potwierdzenia. Do uczenia potrzebne są etykiety oparte na sprawdzeniu kuwety, a nie jedynie interpretacji filmu przez AI.

## 6. Mapa miejsca i przypisanie do kota

1. Opiekun zaznacza obrys podłoża i punkty odniesienia w widoku kamery górnej. Przeliczamy podłoże na stałe współrzędne 0–1, opcjonalnie siatkę 3 × 3.
2. Gdy widać nową plamę, jej maska wyznacza obszar. Gdy plama jest zasłonięta, analiza lokalizuje zad kota w momencie prawdopodobnego oddawania moczu, a nie wyłącznie miejsce późniejszego kopania. Taka lokalizacja jest tylko szacunkiem. Każda kuweta ma osobną kalibrację i mapę.
3. Porównujemy stabilny obraz przed wizytą i po wyjściu; zmiana podłoża jest pomocniczą obserwacją, ponieważ kopanie zmienia obraz niezależnie od moczu.
4. Zapisujemy powiązanie: kot → wizyta → zdarzenie → obszar → materiał dowodowy. Osobno przechowujemy pewność tożsamości, rodzaju zdarzenia i lokalizacji.
5. Na mapie wyświetlamy „prawdopodobne miejsce oddania moczu” lub „potwierdzona bryłka”, odpowiednio do dowodu. Nie przypisujemy jednemu kotu stałego terytorium: ten sam rejon może być używany przez oba koty.
6. Zakopywanie lub następna wizyta mogą przemieścić bryłkę. Historyczne miejsce oddania moczu nie oznacza jej aktualnej lokalizacji. Nakładające się zdarzenia pozostają rozdzielone w historii.
7. Sprzątanie rozpoczyna nowy stan bieżącej mapy, zachowując historię. Przesunięcie kamery lub kuwety wymaga ponownej kalibracji. Gdy miejsca nie widać, zapisujemy „lokalizacja nieznana”.

## 7. Oprogramowanie i przepływ danych

Proponowany stos: Python + FastAPI, OpenCV i mały model wizyjny; FFmpeg do odbioru i zapisu strumieni; SQLite na metadane; pliki MP4/JPEG na SSD; React + TypeScript na interfejs. To wybór projektowy do małej lokalnej instalacji, bez konieczności budowania rozproszonej infrastruktury.

Przepływ: kamery → bufor nagrania → detekcja i tożsamość → sesja wizyty w konkretnej kuwecie → analiza nowej plamy, klipu i opcjonalnych odczytów wagi → zapis zdarzenia i obszaru → aktualizacja aplikacji i powiadomienie.

- Pełny zapis np. 15–20 kl./s, zależnie od kamery; detekcja początkowo 3–5 kl./s, do dostrojenia.
- Bufor np. 15 sekund przed wejściem, cała wizyta i 30 sekund po wyjściu. Krótki brak widoczności nie kończy wizyty.
- Maszyna stanów: pusta → podejście → w kuwecie → wyjście → analiza. Stabilny pusty kadr odróżnia zasłonięcie od rzeczywistego wyjścia.
- Zsynchronizowane zegary kamer/serwera/czujnika, zapis w UTC i wyświetlanie Europe/Warsaw. Czas wizyty liczyć niezależnie od zmiany czasu systemowego.
- Dwa koty jednocześnie, szybka zamiana kotów, człowiek sprzątający i restart w środku wizyty wymagają jawnej obsługi. Nie wymuszać przypisania wyniku, gdy autorstwo jest niejasne.
- Rejestracja ma pierwszeństwo przed analizą; trwała kolejka zadań w bazie pozwala ponowić analizę po restarcie bez podwójnych powiadomień.
- Lokalny login, hasła kamer poza repozytorium, dostęp z LAN; brak publicznego wystawiania panelu i RTSP. Dostęp spoza domu można dodać przez VPN.
- Limit miejsca i retencja, np. 14 dni klipów z zachowaniem ręcznie oznaczonych zdarzeń. Przy dwóch strumieniach po 2 Mb/s zapis całodobowy to około 43 GB/dobę; klipy wizyt znacząco zmniejszą zużycie. To przykład obliczeniowy, nie bitrate konkretnej kamery.
- Informacja o przerwach monitoringu jest częścią danych: kamera offline, PC wyłączony, brak miejsca, nieudana analiza. Nie wyciągać wniosku o braku wizyt w przerwach.

Minimalne dane: koty, kuwety, wizyty, zdarzenia wydalania, obszary, odczyty czujników, pliki dowodowe, korekty użytkownika, okresy niedostępności. Wynik powinien zachować wersję modelu i historię korekt.

## 8. Aplikacja i powiadomienia

Panel zawiera osobne karty Kefira i Kalinki, historię wejść/wyjść, długość wizyt, wynik z poziomem dowodu, dwie mapy kuwet, film oraz obrazy przed wizytą, w chwili pojawienia się plamy i po wyjściu. Historia kota obejmuje obie kuwety. Każdą wizytę można poprawić: zmienić kota, potwierdzić mocz/kał, zaznaczyć obszar lub oznaczyć brak możliwości oceny. Dostępny eksport historii do CSV dla opiekuna lub weterynarza.

Przykładowe komunikaty (dane przykładowe):

> Kefir wszedł do kuwety o 14:32:08.
>
> Kefir wyszedł o 14:33:21. Czas wizyty: 1 min 13 s. Analiza trwa.
>
> Kuweta nr 2: wykryto nową mokrą plamę — prawdopodobne oddanie moczu. Obszar: prawy tył. Zobacz nagranie i zaznaczony rejon.
>
> Opiekun potwierdził mocz po wizycie Kefira o 14:32.

Powiadomienia w otwartym panelu: SSE lub WebSocket. Powiadomienia na zablokowany telefon wymagają osobnego mechanizmu, np. ntfy, i próby na docelowym urządzeniu. W przypadku self-hosted ntfy na iOS natychmiastowe powiadomienia wymagają pośrednictwa upstream i usług push; pełna niezależność od Internetu nie jest wtedy zapewniona. Materiał wideo może nadal pozostawać lokalny. [Dokumentacja ntfy](https://docs.ntfy.sh/config/#ios-instant-notifications)

Alerty: powtarzające się wizyty bez zarejestrowanych oznak moczu, zmiana długości/częstości wizyt, brak zarejestrowanego moczu przez ustawiony okres oraz osobno awaria monitoringu. Progi dotyczące zdrowia ustalić z lekarzem prowadzącym; nie przyjmować uniwersalnej liczby godzin jako bezpiecznej. Liczniki rozdzielają zdarzenia potwierdzone, prawdopodobne i nieznane.

## 9. Etapy implementacji i warunki zakończenia

| Etap | Zakres | Warunek przejścia dalej |
|---|---|---|
| 1. Pilotaż obrazu | Jedna kamera przenoszona testowo między kuwetami, próby kadrów, dzień/noc, lokalny RTSP | Czytelne ujęcia kota i nowej ciemnej plamy; wybór pozycji dla dwóch kamer |
| 2. Rejestrator i panel | Docelowo dwie kamery, nagrania, ręczna tożsamość i etykiety, wejście/wyjście, historia, stan urządzeń | Każda zaobserwowana wizyta ma kuwetę i odtwarzalny klip; przerwy są widoczne |
| 3. Rozpoznawanie kotów | Dane z wielu dni, mały klasyfikator, wynik nieznany | Pomiar błędnych przypisań i nierozpoznanych wizyt osobno dla każdego kota oraz nocy |
| 4. Plamy, mapa i zachowanie | Kalibracja obu kamer, wykrywanie i maski nowych plam, lokalizacja zdarzeń, opcjonalny pilotaż Qwena | Oddzielenie nowych plam od cieni, kopania i starych plam na niezależnych wizytach; jawna niepewność |
| 5. Opcjonalna waga | Jeżeli obraz nie wystarcza: kalibracja pod obciążeniem, synchronizacja, łączenie dowodów | Wykazana przydatność wagi na rzeczywistych wizytach; brak automatycznego „potwierdzenia” z samego przyrostu masy |
| 6. Alerty i próba domowa | Telefon, restart, utrata sieci, pełny dysk, dzienne zestawienia | Sprawdzone dostarczanie i brak duplikatów; wyniki porównane z obserwacją opiekuna |

Planować co najmniej 1–2 tygodnie zbierania naturalnych wizyt; czas może się wydłużyć, jeśli brakuje przykładów ważnych zachowań. Nie prowokować problemów z oddawaniem moczu ani nie ograniczać dostępu do kuwety w celu zdobycia danych. Pierwszy użyteczny rezultat to rejestrator i historia; czas osiągnięcia wiarygodnej oceny moczu zależy od widoczności i danych, nie tylko od programowania.

## 10. Weryfikacja

Zestaw testowy z całych, niezależnych wizyt i późniejszych dni. Etykiety moczu, gdy to możliwe, potwierdzane sprawdzeniem kuwety zaraz po konkretnej wizycie. Nierozstrzygalnych przypadków nie oznaczać jako „brak moczu”.

Mierzyć osobno: pominięte wizyty, pomylone koty, udział „nieznany”, błąd czasu wejścia/wyjścia, trafność i kompletność wykrywania moczu, udział wyników nierozstrzygniętych, dokładność obszaru, opóźnienie powiadomień i dostępność systemu. Każdy wynik podawać z liczbą ocenionych przypadków; kilkanaście dobrych nagrań nie potwierdza niezawodności.

Uwzględnić noc, zasłonięcia, samą eksplorację, kopanie bez wydalania, kał, mocz, szybkie następujące po sobie wizyty i sprzątanie. Fałszywe zapewnienie o oddaniu moczu jest szczególnie niepożądane: przy słabych dowodach wyświetlać niepewność i nagranie do kontroli.

Jeżeli pilot nie pozwoli wiarygodnie odróżniać moczu od wizyty, pozostawić system jako narzędzie obserwacji i historii, a problem pomiaru rozwiązać przed rozwijaniem automatycznych wniosków. Większy model ani trening od zera nie gwarantują rozwiązania.
