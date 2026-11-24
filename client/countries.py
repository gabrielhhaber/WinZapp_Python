"""
Lista de países suportados pelo WhatsApp com seus códigos de discagem,
localizada de acordo com o idioma da interface selecionado pelo usuário.

Cada entrada de _COUNTRIES é uma tupla (chave_estável, código_discagem, nomes):
  • chave_estável: identificador interno, nunca muda (independe de idioma).
  • código_discagem: apenas dígitos, sem o "+".
  • nomes: dict {"pt", "en", "es", "pl", "tr", "ro", "it", "cs"} com o nome do país em
    cada idioma da interface (português vale para pt-BR e pt-PT). Todo país
    deve ter os oito idiomas: tests/test_countries.py garante.

get_countries(lang) devolve [(nome_exibição, código_discagem), ...] ordenada
alfabeticamente pelo NOME LOCALIZADO no idioma pedido (acentos ignorados na
comparação, para que "Áustria"/"Austrália" fiquem ao lado de outras entradas
com "A"). A ordem portanto muda entre idiomas — nenhum país (nem Brasil, nem
o país detectado do Windows) é fixado artificialmente no topo; a posição de
cada item só é usada para indexar de volta na MESMA lista que populou o
ComboBox (ver get_default_country_index()), nunca comparada entre chamadas
com idiomas diferentes.

get_default_country_index() devolve o índice, dentro de uma lista já
devolvida por get_countries(), do país que corresponde a "Country or region"
configurado no Windows (core.locale_format.get_country_or_region_iso2() —
NÃO o idioma de exibição do Windows nem o "Formato regional") — ou dos
Estados Unidos, se essa detecção falhar ou o país detectado não estiver em
_COUNTRIES.

COUNTRIES continua exportado como a lista estática em pt-BR, para código que
só precisa dos códigos de discagem (ex.: core/utils.py) e não da tradução.
"""

from core.utils import normalize_for_search

_LANG_ALIASES = {
    "pt-BR": "pt",
    "pt-PT": "pt",
    "en-US": "en",
    "es-ES": "es",
    "pl": "pl",
    "tr-TR": "tr",
    "ro": "ro",
    "it": "it",
    "cs": "cs",
}

_COUNTRIES: list[tuple[str, str, dict[str, str]]] = [
    # ── Padrão ──────────────────────────────────────────────────────────────
    ("brazil",                      "55",   {"pt": "Brasil", "en": "Brazil", "es": "Brasil", "pl": "Brazylia", "tr": "Brezilya", "ro": "Brazilia", "it": "Brasile", "cs": "Brazílie"}),

    # ── A ──────────────────────────────────────────────────────────────────────
    ("afghanistan",                 "93",   {"pt": "Afeganistão", "en": "Afghanistan", "es": "Afganistán", "pl": "Afganistan", "tr": "Afganistan", "ro": "Afganistan", "it": "Afghanistan", "cs": "Afghánistán"}),
    ("south_africa",                "27",   {"pt": "África do Sul", "en": "South Africa", "es": "Sudáfrica", "pl": "Republika Południowej Afryki", "tr": "Güney Afrika", "ro": "Africa de Sud", "it": "Sudafrica", "cs": "Jihoafrická republika"}),
    ("albania",                     "355",  {"pt": "Albânia", "en": "Albania", "es": "Albania", "pl": "Albania", "tr": "Arnavutluk", "ro": "Albania", "it": "Albania", "cs": "Albánie"}),
    ("germany",                     "49",   {"pt": "Alemanha", "en": "Germany", "es": "Alemania", "pl": "Niemcy", "tr": "Almanya", "ro": "Germania", "it": "Germania", "cs": "Německo"}),
    ("andorra",                     "376",  {"pt": "Andorra", "en": "Andorra", "es": "Andorra", "pl": "Andora", "tr": "Andorra", "ro": "Andorra", "it": "Andorra", "cs": "Andorra"}),
    ("angola",                      "244",  {"pt": "Angola", "en": "Angola", "es": "Angola", "pl": "Angola", "tr": "Angola", "ro": "Angola", "it": "Angola", "cs": "Angola"}),
    ("anguilla",                    "1264", {"pt": "Anguila", "en": "Anguilla", "es": "Anguila", "pl": "Anguilla", "tr": "Anguilla", "ro": "Anguilla", "it": "Anguilla", "cs": "Anguilla"}),
    ("antigua_and_barbuda",         "1268", {"pt": "Antígua e Barbuda", "en": "Antigua and Barbuda", "es": "Antigua y Barbuda", "pl": "Antigua i Barbuda", "tr": "Antigua ve Barbuda", "ro": "Antigua și Barbuda", "it": "Antigua e Barbuda", "cs": "Antigua a Barbuda"}),
    ("saudi_arabia",                "966",  {"pt": "Arábia Saudita", "en": "Saudi Arabia", "es": "Arabia Saudita", "pl": "Arabia Saudyjska", "tr": "Suudi Arabistan", "ro": "Arabia Saudită", "it": "Arabia Saudita", "cs": "Saúdská Arábie"}),
    ("algeria",                     "213",  {"pt": "Argélia", "en": "Algeria", "es": "Argelia", "pl": "Algieria", "tr": "Cezayir", "ro": "Algeria", "it": "Algeria", "cs": "Alžírsko"}),
    ("argentina",                   "54",   {"pt": "Argentina", "en": "Argentina", "es": "Argentina", "pl": "Argentyna", "tr": "Arjantin", "ro": "Argentina", "it": "Argentina", "cs": "Argentina"}),
    ("armenia",                     "374",  {"pt": "Armênia", "en": "Armenia", "es": "Armenia", "pl": "Armenia", "tr": "Ermenistan", "ro": "Armenia", "it": "Armenia", "cs": "Arménie"}),
    ("aruba",                       "297",  {"pt": "Aruba", "en": "Aruba", "es": "Aruba", "pl": "Aruba", "tr": "Aruba", "ro": "Aruba", "it": "Aruba", "cs": "Aruba"}),
    ("australia",                   "61",   {"pt": "Austrália", "en": "Australia", "es": "Australia", "pl": "Australia", "tr": "Avustralya", "ro": "Australia", "it": "Australia", "cs": "Austrálie"}),
    ("austria",                     "43",   {"pt": "Áustria", "en": "Austria", "es": "Austria", "pl": "Austria", "tr": "Avusturya", "ro": "Austria", "it": "Austria", "cs": "Rakousko"}),
    ("azerbaijan",                  "994",  {"pt": "Azerbaijão", "en": "Azerbaijan", "es": "Azerbaiyán", "pl": "Azerbejdżan", "tr": "Azerbaycan", "ro": "Azerbaidjan", "it": "Azerbaigian", "cs": "Ázerbájdžán"}),

    # ── B ──────────────────────────────────────────────────────────────────────
    ("bahamas",                     "1242", {"pt": "Bahamas", "en": "Bahamas", "es": "Bahamas", "pl": "Bahamy", "tr": "Bahamalar", "ro": "Bahamas", "it": "Bahamas", "cs": "Bahamy"}),
    ("bangladesh",                  "880",  {"pt": "Bangladesh", "en": "Bangladesh", "es": "Bangladés", "pl": "Bangladesz", "tr": "Bangladeş", "ro": "Bangladesh", "it": "Bangladesh", "cs": "Bangladéš"}),
    ("barbados",                    "1246", {"pt": "Barbados", "en": "Barbados", "es": "Barbados", "pl": "Barbados", "tr": "Barbados", "ro": "Barbados", "it": "Barbados", "cs": "Barbados"}),
    ("bahrain",                     "973",  {"pt": "Barein", "en": "Bahrain", "es": "Baréin", "pl": "Bahrajn", "tr": "Bahreyn", "ro": "Bahrain", "it": "Bahrein", "cs": "Bahrajn"}),
    ("belgium",                     "32",   {"pt": "Bélgica", "en": "Belgium", "es": "Bélgica", "pl": "Belgia", "tr": "Belçika", "ro": "Belgia", "it": "Belgio", "cs": "Belgie"}),
    ("belize",                      "501",  {"pt": "Belize", "en": "Belize", "es": "Belice", "pl": "Belize", "tr": "Belize", "ro": "Belize", "it": "Belize", "cs": "Belize"}),
    ("benin",                       "229",  {"pt": "Benim", "en": "Benin", "es": "Benín", "pl": "Benin", "tr": "Benin", "ro": "Benin", "it": "Benin", "cs": "Benin"}),
    ("bermuda",                     "1441", {"pt": "Bermudas", "en": "Bermuda", "es": "Bermudas", "pl": "Bermudy", "tr": "Bermuda", "ro": "Bermuda", "it": "Bermuda", "cs": "Bermudy"}),
    ("belarus",                     "375",  {"pt": "Bielorrússia", "en": "Belarus", "es": "Bielorrusia", "pl": "Białoruś", "tr": "Belarus", "ro": "Belarus", "it": "Bielorussia", "cs": "Bělorusko"}),
    ("bolivia",                     "591",  {"pt": "Bolívia", "en": "Bolivia", "es": "Bolivia", "pl": "Boliwia", "tr": "Bolivya", "ro": "Bolivia", "it": "Bolivia", "cs": "Bolívie"}),
    ("bosnia_and_herzegovina",      "387",  {"pt": "Bósnia e Herzegovina", "en": "Bosnia and Herzegovina", "es": "Bosnia y Herzegovina", "pl": "Bośnia i Hercegowina", "tr": "Bosna-Hersek", "ro": "Bosnia și Herțegovina", "it": "Bosnia ed Erzegovina", "cs": "Bosna a Hercegovina"}),
    ("botswana",                    "267",  {"pt": "Botsuana", "en": "Botswana", "es": "Botsuana", "pl": "Botswana", "tr": "Botsvana", "ro": "Botswana", "it": "Botswana", "cs": "Botswana"}),
    ("brunei",                      "673",  {"pt": "Brunei", "en": "Brunei", "es": "Brunéi", "pl": "Brunei", "tr": "Brunei", "ro": "Brunei", "it": "Brunei", "cs": "Brunej"}),
    ("bulgaria",                    "359",  {"pt": "Bulgária", "en": "Bulgaria", "es": "Bulgaria", "pl": "Bułgaria", "tr": "Bulgaristan", "ro": "Bulgaria", "it": "Bulgaria", "cs": "Bulharsko"}),
    ("burkina_faso",                "226",  {"pt": "Burkina Faso", "en": "Burkina Faso", "es": "Burkina Faso", "pl": "Burkina Faso", "tr": "Burkina Faso", "ro": "Burkina Faso", "it": "Burkina Faso", "cs": "Burkina Faso"}),
    ("burundi",                     "257",  {"pt": "Burundi", "en": "Burundi", "es": "Burundi", "pl": "Burundi", "tr": "Burundi", "ro": "Burundi", "it": "Burundi", "cs": "Burundi"}),
    ("bhutan",                      "975",  {"pt": "Butão", "en": "Bhutan", "es": "Bután", "pl": "Bhutan", "tr": "Butan", "ro": "Bhutan", "it": "Bhutan", "cs": "Bhútán"}),

    # ── C ──────────────────────────────────────────────────────────────────────
    ("cape_verde",                  "238",  {"pt": "Cabo Verde", "en": "Cape Verde", "es": "Cabo Verde", "pl": "Republika Zielonego Przylądka", "tr": "Cabo Verde", "ro": "Capul Verde", "it": "Capo Verde", "cs": "Kapverdy"}),
    ("cameroon",                    "237",  {"pt": "Camarões", "en": "Cameroon", "es": "Camerún", "pl": "Kamerun", "tr": "Kamerun", "ro": "Camerun", "it": "Camerun", "cs": "Kamerun"}),
    ("cambodia",                    "855",  {"pt": "Camboja", "en": "Cambodia", "es": "Camboya", "pl": "Kambodża", "tr": "Kamboçya", "ro": "Cambodgia", "it": "Cambogia", "cs": "Kambodža"}),
    ("canada",                      "1",    {"pt": "Canadá", "en": "Canada", "es": "Canadá", "pl": "Kanada", "tr": "Kanada", "ro": "Canada", "it": "Canada", "cs": "Kanada"}),
    ("qatar",                       "974",  {"pt": "Catar", "en": "Qatar", "es": "Catar", "pl": "Katar", "tr": "Katar", "ro": "Qatar", "it": "Qatar", "cs": "Katar"}),
    ("kazakhstan",                  "7",    {"pt": "Cazaquistão", "en": "Kazakhstan", "es": "Kazajistán", "pl": "Kazachstan", "tr": "Kazakistan", "ro": "Kazahstan", "it": "Kazakistan", "cs": "Kazachstán"}),
    ("chad",                        "235",  {"pt": "Chade", "en": "Chad", "es": "Chad", "pl": "Czad", "tr": "Çad", "ro": "Ciad", "it": "Ciad", "cs": "Čad"}),
    ("chile",                       "56",   {"pt": "Chile", "en": "Chile", "es": "Chile", "pl": "Chile", "tr": "Şili", "ro": "Chile", "it": "Cile", "cs": "Chile"}),
    ("china",                       "86",   {"pt": "China", "en": "China", "es": "China", "pl": "Chiny", "tr": "Çin", "ro": "China", "it": "Cina", "cs": "Čína"}),
    ("cyprus",                      "357",  {"pt": "Chipre", "en": "Cyprus", "es": "Chipre", "pl": "Cypr", "tr": "Kıbrıs", "ro": "Cipru", "it": "Cipro", "cs": "Kypr"}),
    ("vatican_city",                "39",   {"pt": "Cidade do Vaticano", "en": "Vatican City", "es": "Ciudad del Vaticano", "pl": "Watykan", "tr": "Vatikan", "ro": "Statul Cetății Vaticanului", "it": "Città del Vaticano", "cs": "Vatikán"}),
    ("colombia",                    "57",   {"pt": "Colômbia", "en": "Colombia", "es": "Colombia", "pl": "Kolumbia", "tr": "Kolombiya", "ro": "Columbia", "it": "Colombia", "cs": "Kolumbie"}),
    ("comoros",                     "269",  {"pt": "Comores", "en": "Comoros", "es": "Comoras", "pl": "Komory", "tr": "Komorlar", "ro": "Comore", "it": "Comore", "cs": "Komory"}),
    ("congo",                       "242",  {"pt": "Congo", "en": "Congo", "es": "Congo", "pl": "Kongo", "tr": "Kongo - Brazavil", "ro": "Congo - Brazzaville", "it": "Congo-Brazzaville", "cs": "Kongo-Brazzaville"}),
    ("north_korea",                 "850",  {"pt": "Coreia do Norte", "en": "North Korea", "es": "Corea del Norte", "pl": "Korea Północna", "tr": "Kuzey Kore", "ro": "Coreea de Nord", "it": "Corea del Nord", "cs": "Severní Korea"}),
    ("south_korea",                 "82",   {"pt": "Coreia do Sul", "en": "South Korea", "es": "Corea del Sur", "pl": "Korea Południowa", "tr": "Güney Kore", "ro": "Coreea de Sud", "it": "Corea del Sud", "cs": "Jižní Korea"}),
    ("ivory_coast",                 "225",  {"pt": "Costa do Marfim", "en": "Ivory Coast", "es": "Costa de Marfil", "pl": "Côte d’Ivoire", "tr": "Côte d’Ivoire", "ro": "Côte d’Ivoire", "it": "Costa d’Avorio", "cs": "Pobřeží slonoviny"}),
    ("costa_rica",                  "506",  {"pt": "Costa Rica", "en": "Costa Rica", "es": "Costa Rica", "pl": "Kostaryka", "tr": "Kosta Rika", "ro": "Costa Rica", "it": "Costa Rica", "cs": "Kostarika"}),
    ("croatia",                     "385",  {"pt": "Croácia", "en": "Croatia", "es": "Croacia", "pl": "Chorwacja", "tr": "Hırvatistan", "ro": "Croația", "it": "Croazia", "cs": "Chorvatsko"}),
    ("cuba",                        "53",   {"pt": "Cuba", "en": "Cuba", "es": "Cuba", "pl": "Kuba", "tr": "Küba", "ro": "Cuba", "it": "Cuba", "cs": "Kuba"}),
    ("curacao",                     "5999", {"pt": "Curaçao", "en": "Curaçao", "es": "Curazao", "pl": "Curaçao", "tr": "Curaçao", "ro": "Curaçao", "it": "Curaçao", "cs": "Curaçao"}),

    # ── D ──────────────────────────────────────────────────────────────────────
    ("denmark",                     "45",   {"pt": "Dinamarca", "en": "Denmark", "es": "Dinamarca", "pl": "Dania", "tr": "Danimarka", "ro": "Danemarca", "it": "Danimarca", "cs": "Dánsko"}),
    ("djibouti",                    "253",  {"pt": "Djibuti", "en": "Djibouti", "es": "Yibuti", "pl": "Dżibuti", "tr": "Cibuti", "ro": "Djibouti", "it": "Gibuti", "cs": "Džibutsko"}),
    ("dominica",                    "1767", {"pt": "Dominica", "en": "Dominica", "es": "Dominica", "pl": "Dominika", "tr": "Dominika", "ro": "Dominica", "it": "Dominica", "cs": "Dominika"}),

    # ── E ──────────────────────────────────────────────────────────────────────
    ("egypt",                       "20",   {"pt": "Egito", "en": "Egypt", "es": "Egipto", "pl": "Egipt", "tr": "Mısır", "ro": "Egipt", "it": "Egitto", "cs": "Egypt"}),
    ("el_salvador",                 "503",  {"pt": "El Salvador", "en": "El Salvador", "es": "El Salvador", "pl": "Salwador", "tr": "El Salvador", "ro": "El Salvador", "it": "El Salvador", "cs": "Salvador"}),
    ("united_arab_emirates",        "971",  {"pt": "Emirados Árabes Unidos", "en": "United Arab Emirates", "es": "Emiratos Árabes Unidos", "pl": "Zjednoczone Emiraty Arabskie", "tr": "Birleşik Arap Emirlikleri", "ro": "Emiratele Arabe Unite", "it": "Emirati Arabi Uniti", "cs": "Spojené arabské emiráty"}),
    ("ecuador",                     "593",  {"pt": "Equador", "en": "Ecuador", "es": "Ecuador", "pl": "Ekwador", "tr": "Ekvador", "ro": "Ecuador", "it": "Ecuador", "cs": "Ekvádor"}),
    ("eritrea",                     "291",  {"pt": "Eritreia", "en": "Eritrea", "es": "Eritrea", "pl": "Erytrea", "tr": "Eritre", "ro": "Eritreea", "it": "Eritrea", "cs": "Eritrea"}),
    ("slovakia",                    "421",  {"pt": "Eslováquia", "en": "Slovakia", "es": "Eslovaquia", "pl": "Słowacja", "tr": "Slovakya", "ro": "Slovacia", "it": "Slovacchia", "cs": "Slovensko"}),
    ("slovenia",                    "386",  {"pt": "Eslovênia", "en": "Slovenia", "es": "Eslovenia", "pl": "Słowenia", "tr": "Slovenya", "ro": "Slovenia", "it": "Slovenia", "cs": "Slovinsko"}),
    ("spain",                       "34",   {"pt": "Espanha", "en": "Spain", "es": "España", "pl": "Hiszpania", "tr": "İspanya", "ro": "Spania", "it": "Spagna", "cs": "Španělsko"}),
    ("united_states",               "1",    {"pt": "Estados Unidos", "en": "United States", "es": "Estados Unidos", "pl": "Stany Zjednoczone", "tr": "Amerika Birleşik Devletleri", "ro": "Statele Unite ale Americii", "it": "Stati Uniti", "cs": "Spojené státy"}),
    ("estonia",                     "372",  {"pt": "Estônia", "en": "Estonia", "es": "Estonia", "pl": "Estonia", "tr": "Estonya", "ro": "Estonia", "it": "Estonia", "cs": "Estonsko"}),
    ("eswatini",                    "268",  {"pt": "Eswatini", "en": "Eswatini", "es": "Esuatini", "pl": "Eswatini", "tr": "Esvatini", "ro": "Eswatini", "it": "Eswatini", "cs": "Esvatini"}),
    ("ethiopia",                    "251",  {"pt": "Etiópia", "en": "Ethiopia", "es": "Etiopía", "pl": "Etiopia", "tr": "Etiyopya", "ro": "Etiopia", "it": "Etiopia", "cs": "Etiopie"}),

    # ── F ──────────────────────────────────────────────────────────────────────
    ("fiji",                        "679",  {"pt": "Fiji", "en": "Fiji", "es": "Fiyi", "pl": "Fidżi", "tr": "Fiji", "ro": "Fiji", "it": "Figi", "cs": "Fidži"}),
    ("philippines",                 "63",   {"pt": "Filipinas", "en": "Philippines", "es": "Filipinas", "pl": "Filipiny", "tr": "Filipinler", "ro": "Filipine", "it": "Filippine", "cs": "Filipíny"}),
    ("finland",                     "358",  {"pt": "Finlândia", "en": "Finland", "es": "Finlandia", "pl": "Finlandia", "tr": "Finlandiya", "ro": "Finlanda", "it": "Finlandia", "cs": "Finsko"}),
    ("france",                      "33",   {"pt": "França", "en": "France", "es": "Francia", "pl": "Francja", "tr": "Fransa", "ro": "Franța", "it": "Francia", "cs": "Francie"}),

    # ── G ──────────────────────────────────────────────────────────────────────
    ("gabon",                       "241",  {"pt": "Gabão", "en": "Gabon", "es": "Gabón", "pl": "Gabon", "tr": "Gabon", "ro": "Gabon", "it": "Gabon", "cs": "Gabon"}),
    ("gambia",                      "220",  {"pt": "Gâmbia", "en": "Gambia", "es": "Gambia", "pl": "Gambia", "tr": "Gambiya", "ro": "Gambia", "it": "Gambia", "cs": "Gambie"}),
    ("ghana",                       "233",  {"pt": "Gana", "en": "Ghana", "es": "Ghana", "pl": "Ghana", "tr": "Gana", "ro": "Ghana", "it": "Ghana", "cs": "Ghana"}),
    ("georgia",                     "995",  {"pt": "Geórgia", "en": "Georgia", "es": "Georgia", "pl": "Gruzja", "tr": "Gürcistan", "ro": "Georgia", "it": "Georgia", "cs": "Gruzie"}),
    ("gibraltar",                   "350",  {"pt": "Gibraltar", "en": "Gibraltar", "es": "Gibraltar", "pl": "Gibraltar", "tr": "Cebelitarık", "ro": "Gibraltar", "it": "Gibilterra", "cs": "Gibraltar"}),
    ("grenada",                     "1473", {"pt": "Granada", "en": "Grenada", "es": "Granada", "pl": "Grenada", "tr": "Grenada", "ro": "Grenada", "it": "Grenada", "cs": "Grenada"}),
    ("greece",                      "30",   {"pt": "Grécia", "en": "Greece", "es": "Grecia", "pl": "Grecja", "tr": "Yunanistan", "ro": "Grecia", "it": "Grecia", "cs": "Řecko"}),
    ("greenland",                   "299",  {"pt": "Groenlândia", "en": "Greenland", "es": "Groenlandia", "pl": "Grenlandia", "tr": "Grönland", "ro": "Groenlanda", "it": "Groenlandia", "cs": "Grónsko"}),
    ("guadeloupe",                  "590",  {"pt": "Guadalupe", "en": "Guadeloupe", "es": "Guadalupe", "pl": "Gwadelupa", "tr": "Guadeloupe", "ro": "Guadelupa", "it": "Guadalupa", "cs": "Guadeloupe"}),
    ("guam",                        "1671", {"pt": "Guam", "en": "Guam", "es": "Guam", "pl": "Guam", "tr": "Guam", "ro": "Guam", "it": "Guam", "cs": "Guam"}),
    ("guatemala",                   "502",  {"pt": "Guatemala", "en": "Guatemala", "es": "Guatemala", "pl": "Gwatemala", "tr": "Guatemala", "ro": "Guatemala", "it": "Guatemala", "cs": "Guatemala"}),
    ("guernsey",                    "44",   {"pt": "Guernsey", "en": "Guernsey", "es": "Guernesey", "pl": "Guernsey", "tr": "Guernsey", "ro": "Guernsey", "it": "Guernsey", "cs": "Guernsey"}),
    ("guyana",                      "592",  {"pt": "Guiana", "en": "Guyana", "es": "Guyana", "pl": "Gujana", "tr": "Guyana", "ro": "Guyana", "it": "Guyana", "cs": "Guyana"}),
    ("french_guiana",               "594",  {"pt": "Guiana Francesa", "en": "French Guiana", "es": "Guayana Francesa", "pl": "Gujana Francuska", "tr": "Fransız Guyanası", "ro": "Guyana Franceză", "it": "Guyana Francese", "cs": "Francouzská Guyana"}),
    ("guinea",                      "224",  {"pt": "Guiné", "en": "Guinea", "es": "Guinea", "pl": "Gwinea", "tr": "Gine", "ro": "Guineea", "it": "Guinea", "cs": "Guinea"}),
    ("guinea_bissau",               "245",  {"pt": "Guiné Bissau", "en": "Guinea-Bissau", "es": "Guinea-Bisáu", "pl": "Gwinea Bissau", "tr": "Gine-Bissau", "ro": "Guineea-Bissau", "it": "Guinea-Bissau", "cs": "Guinea-Bissau"}),
    ("equatorial_guinea",           "240",  {"pt": "Guiné Equatorial", "en": "Equatorial Guinea", "es": "Guinea Ecuatorial", "pl": "Gwinea Równikowa", "tr": "Ekvator Ginesi", "ro": "Guineea Ecuatorială", "it": "Guinea Equatoriale", "cs": "Rovníková Guinea"}),

    # ── H ──────────────────────────────────────────────────────────────────────
    ("haiti",                       "509",  {"pt": "Haiti", "en": "Haiti", "es": "Haití", "pl": "Haiti", "tr": "Haiti", "ro": "Haiti", "it": "Haiti", "cs": "Haiti"}),
    ("honduras",                    "504",  {"pt": "Honduras", "en": "Honduras", "es": "Honduras", "pl": "Honduras", "tr": "Honduras", "ro": "Honduras", "it": "Honduras", "cs": "Honduras"}),
    ("hong_kong",                   "852",  {"pt": "Hong Kong", "en": "Hong Kong", "es": "Hong Kong", "pl": "SRA Hongkong (Chiny)", "tr": "Çin Hong Kong ÖİB", "ro": "R.A.S. Hong Kong, China", "it": "RAS di Hong Kong", "cs": "Hongkong"}),
    ("hungary",                     "36",   {"pt": "Hungria", "en": "Hungary", "es": "Hungría", "pl": "Węgry", "tr": "Macaristan", "ro": "Ungaria", "it": "Ungheria", "cs": "Maďarsko"}),

    # ── I ──────────────────────────────────────────────────────────────────────
    ("yemen",                       "967",  {"pt": "Iêmen", "en": "Yemen", "es": "Yemen", "pl": "Jemen", "tr": "Yemen", "ro": "Yemen", "it": "Yemen", "cs": "Jemen"}),
    ("ascension_island",            "247",  {"pt": "Ilha de Ascensão", "en": "Ascension Island", "es": "Isla de la Ascensión", "pl": "Wyspa Wniebowstąpienia", "tr": "Ascension Adası", "ro": "Insula Ascension", "it": "Isola Ascensione", "cs": "Ostrov Ascension"}),
    ("isle_of_man",                 "44",   {"pt": "Ilha de Man", "en": "Isle of Man", "es": "Isla de Man", "pl": "Wyspa Man", "tr": "Man Adası", "ro": "Insula Man", "it": "Isola di Man", "cs": "Ostrov Man"}),
    ("norfolk_island",              "672",  {"pt": "Ilha Norfolk", "en": "Norfolk Island", "es": "Isla Norfolk", "pl": "Norfolk", "tr": "Norfolk Adası", "ro": "Insula Norfolk", "it": "Isola Norfolk", "cs": "Norfolk"}),
    ("cayman_islands",              "1345", {"pt": "Ilhas Cayman", "en": "Cayman Islands", "es": "Islas Caimán", "pl": "Kajmany", "tr": "Cayman Adaları", "ro": "Insulele Cayman", "it": "Isole Cayman", "cs": "Kajmanské ostrovy"}),
    ("cook_islands",                "682",  {"pt": "Ilhas Cook", "en": "Cook Islands", "es": "Islas Cook", "pl": "Wyspy Cooka", "tr": "Cook Adaları", "ro": "Insulele Cook", "it": "Isole Cook", "cs": "Cookovy ostrovy"}),
    ("faroe_islands",               "298",  {"pt": "Ilhas Faroe", "en": "Faroe Islands", "es": "Islas Feroe", "pl": "Wyspy Owcze", "tr": "Faroe Adaları", "ro": "Insulele Feroe", "it": "Isole Fær Øer", "cs": "Faerské ostrovy"}),
    ("falkland_islands",            "500",  {"pt": "Ilhas Malvinas", "en": "Falkland Islands", "es": "Islas Malvinas", "pl": "Falklandy", "tr": "Falkland Adaları", "ro": "Insulele Falkland", "it": "Isole Falkland", "cs": "Falklandy"}),
    ("northern_mariana_islands",    "1670", {"pt": "Ilhas Marianas do Norte", "en": "Northern Mariana Islands", "es": "Islas Marianas del Norte", "pl": "Mariany Północne", "tr": "Kuzey Mariana Adaları", "ro": "Insulele Mariane de Nord", "it": "Isole Marianne Settentrionali", "cs": "Severní Mariany"}),
    ("marshall_islands",            "692",  {"pt": "Ilhas Marshall", "en": "Marshall Islands", "es": "Islas Marshall", "pl": "Wyspy Marshalla", "tr": "Marshall Adaları", "ro": "Insulele Marshall", "it": "Isole Marshall", "cs": "Marshallovy ostrovy"}),
    ("solomon_islands",             "677",  {"pt": "Ilhas Salomão", "en": "Solomon Islands", "es": "Islas Salomón", "pl": "Wyspy Salomona", "tr": "Solomon Adaları", "ro": "Insulele Solomon", "it": "Isole Salomone", "cs": "Šalamounovy ostrovy"}),
    ("turks_and_caicos_islands",    "1649", {"pt": "Ilhas Turks e Caicos", "en": "Turks and Caicos Islands", "es": "Islas Turcas y Caicos", "pl": "Turks i Caicos", "tr": "Turks ve Caicos Adaları", "ro": "Insulele Turks și Caicos", "it": "Isole Turks e Caicos", "cs": "Turks a Caicos"}),
    ("british_virgin_islands",      "1284", {"pt": "Ilhas Virgens Britânicas", "en": "British Virgin Islands", "es": "Islas Vírgenes Británicas", "pl": "Brytyjskie Wyspy Dziewicze", "tr": "Britanya Virjin Adaları", "ro": "Insulele Virgine Britanice", "it": "Isole Vergini Britanniche", "cs": "Britské Panenské ostrovy"}),
    ("us_virgin_islands",           "1340", {"pt": "Ilhas Virgens dos EUA", "en": "U.S. Virgin Islands", "es": "Islas Vírgenes de EE. UU.", "pl": "Wyspy Dziewicze Stanów Zjednoczonych", "tr": "ABD Virjin Adaları", "ro": "Insulele Virgine Americane", "it": "Isole Vergini Americane", "cs": "Americké Panenské ostrovy"}),
    ("india",                       "91",   {"pt": "Índia", "en": "India", "es": "India", "pl": "Indie", "tr": "Hindistan", "ro": "India", "it": "India", "cs": "Indie"}),
    ("indonesia",                   "62",   {"pt": "Indonésia", "en": "Indonesia", "es": "Indonesia", "pl": "Indonezja", "tr": "Endonezya", "ro": "Indonezia", "it": "Indonesia", "cs": "Indonésie"}),
    ("iran",                        "98",   {"pt": "Irã", "en": "Iran", "es": "Irán", "pl": "Iran", "tr": "İran", "ro": "Iran", "it": "Iran", "cs": "Írán"}),
    ("iraq",                        "964",  {"pt": "Iraque", "en": "Iraq", "es": "Irak", "pl": "Irak", "tr": "Irak", "ro": "Irak", "it": "Iraq", "cs": "Irák"}),
    ("ireland",                     "353",  {"pt": "Irlanda", "en": "Ireland", "es": "Irlanda", "pl": "Irlandia", "tr": "İrlanda", "ro": "Irlanda", "it": "Irlanda", "cs": "Irsko"}),
    ("iceland",                     "354",  {"pt": "Islândia", "en": "Iceland", "es": "Islandia", "pl": "Islandia", "tr": "İzlanda", "ro": "Islanda", "it": "Islanda", "cs": "Island"}),
    ("israel",                      "972",  {"pt": "Israel", "en": "Israel", "es": "Israel", "pl": "Izrael", "tr": "İsrail", "ro": "Israel", "it": "Israele", "cs": "Izrael"}),
    ("italy",                       "39",   {"pt": "Itália", "en": "Italy", "es": "Italia", "pl": "Włochy", "tr": "İtalya", "ro": "Italia", "it": "Italia", "cs": "Itálie"}),

    # ── J ──────────────────────────────────────────────────────────────────────
    ("jamaica",                     "1876", {"pt": "Jamaica", "en": "Jamaica", "es": "Jamaica", "pl": "Jamajka", "tr": "Jamaika", "ro": "Jamaica", "it": "Giamaica", "cs": "Jamajka"}),
    ("japan",                       "81",   {"pt": "Japão", "en": "Japan", "es": "Japón", "pl": "Japonia", "tr": "Japonya", "ro": "Japonia", "it": "Giappone", "cs": "Japonsko"}),
    ("jersey",                      "44",   {"pt": "Jersey", "en": "Jersey", "es": "Jersey", "pl": "Jersey", "tr": "Jersey", "ro": "Jersey", "it": "Jersey", "cs": "Jersey"}),
    ("jordan",                      "962",  {"pt": "Jordânia", "en": "Jordan", "es": "Jordania", "pl": "Jordania", "tr": "Ürdün", "ro": "Iordania", "it": "Giordania", "cs": "Jordánsko"}),

    # ── K ──────────────────────────────────────────────────────────────────────
    ("kosovo",                      "383",  {"pt": "Kosovo", "en": "Kosovo", "es": "Kosovo", "pl": "Kosowo", "tr": "Kosova", "ro": "Kosovo", "it": "Kosovo", "cs": "Kosovo"}),
    ("kuwait",                      "965",  {"pt": "Kuwait", "en": "Kuwait", "es": "Kuwait", "pl": "Kuwejt", "tr": "Kuveyt", "ro": "Kuweit", "it": "Kuwait", "cs": "Kuvajt"}),

    # ── L ──────────────────────────────────────────────────────────────────────
    ("laos",                        "856",  {"pt": "Laos", "en": "Laos", "es": "Laos", "pl": "Laos", "tr": "Laos", "ro": "Laos", "it": "Laos", "cs": "Laos"}),
    ("lesotho",                     "266",  {"pt": "Lesoto", "en": "Lesotho", "es": "Lesoto", "pl": "Lesotho", "tr": "Lesotho", "ro": "Lesotho", "it": "Lesotho", "cs": "Lesotho"}),
    ("latvia",                      "371",  {"pt": "Letônia", "en": "Latvia", "es": "Letonia", "pl": "Łotwa", "tr": "Letonya", "ro": "Letonia", "it": "Lettonia", "cs": "Lotyšsko"}),
    ("lebanon",                     "961",  {"pt": "Líbano", "en": "Lebanon", "es": "Líbano", "pl": "Liban", "tr": "Lübnan", "ro": "Liban", "it": "Libano", "cs": "Libanon"}),
    ("liberia",                     "231",  {"pt": "Libéria", "en": "Liberia", "es": "Liberia", "pl": "Liberia", "tr": "Liberya", "ro": "Liberia", "it": "Liberia", "cs": "Libérie"}),
    ("libya",                       "218",  {"pt": "Líbia", "en": "Libya", "es": "Libia", "pl": "Libia", "tr": "Libya", "ro": "Libia", "it": "Libia", "cs": "Libye"}),
    ("liechtenstein",               "423",  {"pt": "Liechtenstein", "en": "Liechtenstein", "es": "Liechtenstein", "pl": "Liechtenstein", "tr": "Liechtenstein", "ro": "Liechtenstein", "it": "Liechtenstein", "cs": "Lichtenštejnsko"}),
    ("lithuania",                   "370",  {"pt": "Lituânia", "en": "Lithuania", "es": "Lituania", "pl": "Litwa", "tr": "Litvanya", "ro": "Lituania", "it": "Lituania", "cs": "Litva"}),
    ("luxembourg",                  "352",  {"pt": "Luxemburgo", "en": "Luxembourg", "es": "Luxemburgo", "pl": "Luksemburg", "tr": "Lüksemburg", "ro": "Luxemburg", "it": "Lussemburgo", "cs": "Lucembursko"}),

    # ── M ──────────────────────────────────────────────────────────────────────
    ("macau",                       "853",  {"pt": "Macau", "en": "Macau", "es": "Macao", "pl": "SRA Makau (Chiny)", "tr": "Çin Makao ÖİB", "ro": "R.A.S. Macao, China", "it": "RAS di Macao", "cs": "Macao"}),
    ("north_macedonia",             "389",  {"pt": "Macedônia do Norte", "en": "North Macedonia", "es": "Macedonia del Norte", "pl": "Macedonia Północna", "tr": "Kuzey Makedonya", "ro": "Macedonia de Nord", "it": "Macedonia del Nord", "cs": "Severní Makedonie"}),
    ("madagascar",                  "261",  {"pt": "Madagascar", "en": "Madagascar", "es": "Madagascar", "pl": "Madagaskar", "tr": "Madagaskar", "ro": "Madagascar", "it": "Madagascar", "cs": "Madagaskar"}),
    ("malaysia",                    "60",   {"pt": "Malásia", "en": "Malaysia", "es": "Malasia", "pl": "Malezja", "tr": "Malezya", "ro": "Malaysia", "it": "Malaysia", "cs": "Malajsie"}),
    ("malawi",                      "265",  {"pt": "Malawi", "en": "Malawi", "es": "Malaui", "pl": "Malawi", "tr": "Malavi", "ro": "Malawi", "it": "Malawi", "cs": "Malawi"}),
    ("maldives",                    "960",  {"pt": "Maldivas", "en": "Maldives", "es": "Maldivas", "pl": "Malediwy", "tr": "Maldivler", "ro": "Maldive", "it": "Maldive", "cs": "Maledivy"}),
    ("mali",                        "223",  {"pt": "Mali", "en": "Mali", "es": "Malí", "pl": "Mali", "tr": "Mali", "ro": "Mali", "it": "Mali", "cs": "Mali"}),
    ("malta",                       "356",  {"pt": "Malta", "en": "Malta", "es": "Malta", "pl": "Malta", "tr": "Malta", "ro": "Malta", "it": "Malta", "cs": "Malta"}),
    ("morocco",                     "212",  {"pt": "Marrocos", "en": "Morocco", "es": "Marruecos", "pl": "Maroko", "tr": "Fas", "ro": "Maroc", "it": "Marocco", "cs": "Maroko"}),
    ("martinique",                  "596",  {"pt": "Martinica", "en": "Martinique", "es": "Martinica", "pl": "Martynika", "tr": "Martinik", "ro": "Martinica", "it": "Martinica", "cs": "Martinik"}),
    ("mauritius",                   "230",  {"pt": "Maurício", "en": "Mauritius", "es": "Mauricio", "pl": "Mauritius", "tr": "Mauritius", "ro": "Mauritius", "it": "Mauritius", "cs": "Mauricius"}),
    ("mauritania",                  "222",  {"pt": "Mauritânia", "en": "Mauritania", "es": "Mauritania", "pl": "Mauretania", "tr": "Moritanya", "ro": "Mauritania", "it": "Mauritania", "cs": "Mauritánie"}),
    ("mayotte",                     "262",  {"pt": "Mayotte", "en": "Mayotte", "es": "Mayotte", "pl": "Majotta", "tr": "Mayotte", "ro": "Mayotte", "it": "Mayotte", "cs": "Mayotte"}),
    ("mexico",                      "52",   {"pt": "México", "en": "Mexico", "es": "México", "pl": "Meksyk", "tr": "Meksika", "ro": "Mexic", "it": "Messico", "cs": "Mexiko"}),
    ("myanmar",                     "95",   {"pt": "Mianmar", "en": "Myanmar", "es": "Myanmar", "pl": "Mjanma (Birma)", "tr": "Myanmar (Burma)", "ro": "Myanmar (Birmania)", "it": "Myanmar (Birmania)", "cs": "Myanmar"}),
    ("micronesia",                  "691",  {"pt": "Micronésia", "en": "Micronesia", "es": "Micronesia", "pl": "Mikronezja", "tr": "Mikronezya", "ro": "Micronezia", "it": "Micronesia", "cs": "Mikronésie"}),
    ("mozambique",                  "258",  {"pt": "Moçambique", "en": "Mozambique", "es": "Mozambique", "pl": "Mozambik", "tr": "Mozambik", "ro": "Mozambic", "it": "Mozambico", "cs": "Mosambik"}),
    ("moldova",                     "373",  {"pt": "Moldávia", "en": "Moldova", "es": "Moldavia", "pl": "Mołdawia", "tr": "Moldova", "ro": "Republica Moldova", "it": "Moldavia", "cs": "Moldavsko"}),
    ("monaco",                      "377",  {"pt": "Mônaco", "en": "Monaco", "es": "Mónaco", "pl": "Monako", "tr": "Monako", "ro": "Monaco", "it": "Monaco", "cs": "Monako"}),
    ("mongolia",                    "976",  {"pt": "Mongólia", "en": "Mongolia", "es": "Mongolia", "pl": "Mongolia", "tr": "Moğolistan", "ro": "Mongolia", "it": "Mongolia", "cs": "Mongolsko"}),
    ("montenegro",                  "382",  {"pt": "Montenegro", "en": "Montenegro", "es": "Montenegro", "pl": "Czarnogóra", "tr": "Karadağ", "ro": "Muntenegru", "it": "Montenegro", "cs": "Černá Hora"}),
    ("montserrat",                  "1664", {"pt": "Montserrat", "en": "Montserrat", "es": "Montserrat", "pl": "Montserrat", "tr": "Montserrat", "ro": "Montserrat", "it": "Montserrat", "cs": "Montserrat"}),

    # ── N ──────────────────────────────────────────────────────────────────────
    ("namibia",                     "264",  {"pt": "Namíbia", "en": "Namibia", "es": "Namibia", "pl": "Namibia", "tr": "Namibya", "ro": "Namibia", "it": "Namibia", "cs": "Namibie"}),
    ("nauru",                       "674",  {"pt": "Nauru", "en": "Nauru", "es": "Nauru", "pl": "Nauru", "tr": "Nauru", "ro": "Nauru", "it": "Nauru", "cs": "Nauru"}),
    ("nepal",                       "977",  {"pt": "Nepal", "en": "Nepal", "es": "Nepal", "pl": "Nepal", "tr": "Nepal", "ro": "Nepal", "it": "Nepal", "cs": "Nepál"}),
    ("nicaragua",                   "505",  {"pt": "Nicarágua", "en": "Nicaragua", "es": "Nicaragua", "pl": "Nikaragua", "tr": "Nikaragua", "ro": "Nicaragua", "it": "Nicaragua", "cs": "Nikaragua"}),
    ("niger",                       "227",  {"pt": "Níger", "en": "Niger", "es": "Níger", "pl": "Niger", "tr": "Nijer", "ro": "Niger", "it": "Niger", "cs": "Niger"}),
    ("nigeria",                     "234",  {"pt": "Nigéria", "en": "Nigeria", "es": "Nigeria", "pl": "Nigeria", "tr": "Nijerya", "ro": "Nigeria", "it": "Nigeria", "cs": "Nigérie"}),
    ("niue",                        "683",  {"pt": "Niue", "en": "Niue", "es": "Niue", "pl": "Niue", "tr": "Niue", "ro": "Niue", "it": "Niue", "cs": "Niue"}),
    ("norway",                      "47",   {"pt": "Noruega", "en": "Norway", "es": "Noruega", "pl": "Norwegia", "tr": "Norveç", "ro": "Norvegia", "it": "Norvegia", "cs": "Norsko"}),
    ("new_caledonia",               "687",  {"pt": "Nova Caledônia", "en": "New Caledonia", "es": "Nueva Caledonia", "pl": "Nowa Kaledonia", "tr": "Yeni Kaledonya", "ro": "Noua Caledonie", "it": "Nuova Caledonia", "cs": "Nová Kaledonie"}),
    ("new_zealand",                 "64",   {"pt": "Nova Zelândia", "en": "New Zealand", "es": "Nueva Zelanda", "pl": "Nowa Zelandia", "tr": "Yeni Zelanda", "ro": "Noua Zeelandă", "it": "Nuova Zelanda", "cs": "Nový Zéland"}),

    # ── O ──────────────────────────────────────────────────────────────────────
    ("oman",                        "968",  {"pt": "Omã", "en": "Oman", "es": "Omán", "pl": "Oman", "tr": "Umman", "ro": "Oman", "it": "Oman", "cs": "Omán"}),

    # ── P ──────────────────────────────────────────────────────────────────────
    ("netherlands",                 "31",   {"pt": "Países Baixos", "en": "Netherlands", "es": "Países Bajos", "pl": "Holandia", "tr": "Hollanda", "ro": "Țările de Jos", "it": "Paesi Bassi", "cs": "Nizozemsko"}),
    ("caribbean_netherlands",       "599",  {"pt": "Países Baixos Caribenhos", "en": "Caribbean Netherlands", "es": "Caribe neerlandés", "pl": "Niderlandy Karaibskie", "tr": "Karayip Hollandası", "ro": "Insulele Caraibe Olandeze", "it": "Caraibi Olandesi", "cs": "Karibské Nizozemsko"}),
    ("palau",                       "680",  {"pt": "Palau", "en": "Palau", "es": "Palaos", "pl": "Palau", "tr": "Palau", "ro": "Palau", "it": "Palau", "cs": "Palau"}),
    ("panama",                      "507",  {"pt": "Panamá", "en": "Panama", "es": "Panamá", "pl": "Panama", "tr": "Panama", "ro": "Panama", "it": "Panama", "cs": "Panama"}),
    ("papua_new_guinea",            "675",  {"pt": "Papua Nova Guiné", "en": "Papua New Guinea", "es": "Papúa Nueva Guinea", "pl": "Papua-Nowa Gwinea", "tr": "Papua Yeni Gine", "ro": "Papua-Noua Guinee", "it": "Papua Nuova Guinea", "cs": "Papua-Nová Guinea"}),
    ("pakistan",                    "92",   {"pt": "Paquistão", "en": "Pakistan", "es": "Pakistán", "pl": "Pakistan", "tr": "Pakistan", "ro": "Pakistan", "it": "Pakistan", "cs": "Pákistán"}),
    ("paraguay",                    "595",  {"pt": "Paraguai", "en": "Paraguay", "es": "Paraguay", "pl": "Paragwaj", "tr": "Paraguay", "ro": "Paraguay", "it": "Paraguay", "cs": "Paraguay"}),
    ("peru",                        "51",   {"pt": "Peru", "en": "Peru", "es": "Perú", "pl": "Peru", "tr": "Peru", "ro": "Peru", "it": "Perù", "cs": "Peru"}),
    ("french_polynesia",            "689",  {"pt": "Polinésia Francesa", "en": "French Polynesia", "es": "Polinesia Francesa", "pl": "Polinezja Francuska", "tr": "Fransız Polinezyası", "ro": "Polinezia Franceză", "it": "Polinesia Francese", "cs": "Francouzská Polynésie"}),
    ("poland",                      "48",   {"pt": "Polônia", "en": "Poland", "es": "Polonia", "pl": "Polska", "tr": "Polonya", "ro": "Polonia", "it": "Polonia", "cs": "Polsko"}),
    ("puerto_rico",                 "1787", {"pt": "Porto Rico", "en": "Puerto Rico", "es": "Puerto Rico", "pl": "Portoryko", "tr": "Porto Riko", "ro": "Puerto Rico", "it": "Portorico", "cs": "Portoriko"}),
    ("portugal",                    "351",  {"pt": "Portugal", "en": "Portugal", "es": "Portugal", "pl": "Portugalia", "tr": "Portekiz", "ro": "Portugalia", "it": "Portogallo", "cs": "Portugalsko"}),

    # ── Q ──────────────────────────────────────────────────────────────────────
    ("kenya",                       "254",  {"pt": "Quênia", "en": "Kenya", "es": "Kenia", "pl": "Kenia", "tr": "Kenya", "ro": "Kenya", "it": "Kenya", "cs": "Keňa"}),
    ("kyrgyzstan",                  "996",  {"pt": "Quirguistão", "en": "Kyrgyzstan", "es": "Kirguistán", "pl": "Kirgistan", "tr": "Kırgızistan", "ro": "Kârgâzstan", "it": "Kirghizistan", "cs": "Kyrgyzstán"}),
    ("kiribati",                    "686",  {"pt": "Quiribati", "en": "Kiribati", "es": "Kiribati", "pl": "Kiribati", "tr": "Kiribati", "ro": "Kiribati", "it": "Kiribati", "cs": "Kiribati"}),

    # ── R ──────────────────────────────────────────────────────────────────────
    ("united_kingdom",              "44",   {"pt": "Reino Unido", "en": "United Kingdom", "es": "Reino Unido", "pl": "Wielka Brytania", "tr": "Birleşik Krallık", "ro": "Regatul Unit", "it": "Regno Unito", "cs": "Spojené království"}),
    ("central_african_republic",    "236",  {"pt": "República Centro-Africana", "en": "Central African Republic", "es": "República Centroafricana", "pl": "Republika Środkowoafrykańska", "tr": "Orta Afrika Cumhuriyeti", "ro": "Republica Centrafricană", "it": "Repubblica Centrafricana", "cs": "Středoafrická republika"}),
    ("democratic_republic_congo",   "243",  {"pt": "República Democrática do Congo", "en": "Democratic Republic of the Congo", "es": "República Democrática del Congo", "pl": "Demokratyczna Republika Konga", "tr": "Kongo - Kinşasa", "ro": "Congo - Kinshasa", "it": "Congo - Kinshasa", "cs": "Demokratická republika Kongo"}),
    ("dominican_republic",          "1809", {"pt": "República Dominicana", "en": "Dominican Republic", "es": "República Dominicana", "pl": "Dominikana", "tr": "Dominik Cumhuriyeti", "ro": "Republica Dominicană", "it": "Repubblica Dominicana", "cs": "Dominikánská republika"}),
    ("czech_republic",              "420",  {"pt": "República Tcheca", "en": "Czech Republic", "es": "República Checa", "pl": "Czechy", "tr": "Çekya", "ro": "Cehia", "it": "Cechia", "cs": "Česko"}),
    ("reunion",                     "262",  {"pt": "Reunião", "en": "Réunion", "es": "Reunión", "pl": "Reunion", "tr": "Reunion", "ro": "Réunion", "it": "Riunione", "cs": "Réunion"}),
    ("romania",                     "40",   {"pt": "Romênia", "en": "Romania", "es": "Rumanía", "pl": "Rumunia", "tr": "Romanya", "ro": "România", "it": "Romania", "cs": "Rumunsko"}),
    ("rwanda",                      "250",  {"pt": "Ruanda", "en": "Rwanda", "es": "Ruanda", "pl": "Rwanda", "tr": "Ruanda", "ro": "Rwanda", "it": "Ruanda", "cs": "Rwanda"}),
    ("russia",                      "7",    {"pt": "Rússia", "en": "Russia", "es": "Rusia", "pl": "Rosja", "tr": "Rusya", "ro": "Rusia", "it": "Russia", "cs": "Rusko"}),

    # ── S ──────────────────────────────────────────────────────────────────────
    ("samoa",                       "685",  {"pt": "Samoa", "en": "Samoa", "es": "Samoa", "pl": "Samoa", "tr": "Samoa", "ro": "Samoa", "it": "Samoa", "cs": "Samoa"}),
    ("american_samoa",              "1684", {"pt": "Samoa Americana", "en": "American Samoa", "es": "Samoa Americana", "pl": "Samoa Amerykańskie", "tr": "Amerikan Samoası", "ro": "Samoa Americană", "it": "Samoa Americane", "cs": "Americká Samoa"}),
    ("san_marino",                  "378",  {"pt": "San Marino", "en": "San Marino", "es": "San Marino", "pl": "San Marino", "tr": "San Marino", "ro": "San Marino", "it": "San Marino", "cs": "San Marino"}),
    ("saint_helena",                "290",  {"pt": "Santa Helena", "en": "St. Helena", "es": "Santa Elena", "pl": "Wyspa Świętej Heleny", "tr": "Saint Helena", "ro": "Sfânta Elena", "it": "Sant’Elena", "cs": "Svatá Helena"}),
    ("saint_lucia",                 "1758", {"pt": "Santa Lúcia", "en": "Saint Lucia", "es": "Santa Lucía", "pl": "Saint Lucia", "tr": "Saint Lucia", "ro": "Sfânta Lucia", "it": "Saint Lucia", "cs": "Svatá Lucie"}),
    ("saint_barthelemy",            "590",  {"pt": "São Bartolomeu", "en": "St. Barthélemy", "es": "San Bartolomé", "pl": "Saint-Barthélemy", "tr": "Saint Barthelemy", "ro": "Saint-Barthélemy", "it": "Saint-Barthélemy", "cs": "Svatý Bartoloměj"}),
    ("saint_kitts_and_nevis",       "1869", {"pt": "São Cristóvão e Nevis", "en": "Saint Kitts and Nevis", "es": "San Cristóbal y Nieves", "pl": "Saint Kitts i Nevis", "tr": "Saint Kitts ve Nevis", "ro": "Saint Kitts și Nevis", "it": "Saint Kitts e Nevis", "cs": "Svatý Kryštof a Nevis"}),
    ("saint_martin",                "590",  {"pt": "São Martinho", "en": "St. Martin", "es": "San Martín", "pl": "Saint-Martin", "tr": "Saint Martin", "ro": "Sfântul Martin", "it": "Saint Martin", "cs": "Svatý Martin"}),
    ("saint_pierre_and_miquelon",   "508",  {"pt": "São Pedro e Miquelão", "en": "St. Pierre & Miquelon", "es": "San Pedro y Miquelón", "pl": "Saint-Pierre i Miquelon", "tr": "Saint Pierre ve Miquelon", "ro": "Saint-Pierre și Miquelon", "it": "Saint-Pierre e Miquelon", "cs": "Saint-Pierre a Miquelon"}),
    ("sao_tome_and_principe",       "239",  {"pt": "São Tomé e Príncipe", "en": "São Tomé and Príncipe", "es": "Santo Tomé y Príncipe", "pl": "Wyspy Świętego Tomasza i Książęca", "tr": "Sao Tome ve Principe", "ro": "São Tomé și Príncipe", "it": "São Tomé e Príncipe", "cs": "Svatý Tomáš a Princův ostrov"}),
    ("saint_vincent_grenadines",    "1784", {"pt": "São Vicente e Granadinas", "en": "Saint Vincent and the Grenadines", "es": "San Vicente y las Granadinas", "pl": "Saint Vincent i Grenadyny", "tr": "Saint Vincent ve Grenadinler", "ro": "Saint Vincent și Grenadinele", "it": "Saint Vincent e Grenadine", "cs": "Svatý Vincenc a Grenadiny"}),
    ("senegal",                     "221",  {"pt": "Senegal", "en": "Senegal", "es": "Senegal", "pl": "Senegal", "tr": "Senegal", "ro": "Senegal", "it": "Senegal", "cs": "Senegal"}),
    ("sierra_leone",                "232",  {"pt": "Serra Leoa", "en": "Sierra Leone", "es": "Sierra Leona", "pl": "Sierra Leone", "tr": "Sierra Leone", "ro": "Sierra Leone", "it": "Sierra Leone", "cs": "Sierra Leone"}),
    ("serbia",                      "381",  {"pt": "Sérvia", "en": "Serbia", "es": "Serbia", "pl": "Serbia", "tr": "Sırbistan", "ro": "Serbia", "it": "Serbia", "cs": "Srbsko"}),
    ("seychelles",                  "248",  {"pt": "Seychelles", "en": "Seychelles", "es": "Seychelles", "pl": "Seszele", "tr": "Seyşeller", "ro": "Seychelles", "it": "Seychelles", "cs": "Seychely"}),
    ("singapore",                   "65",   {"pt": "Singapura", "en": "Singapore", "es": "Singapur", "pl": "Singapur", "tr": "Singapur", "ro": "Singapore", "it": "Singapore", "cs": "Singapur"}),
    ("sint_maarten",                "1721", {"pt": "Sint Maarten", "en": "Sint Maarten", "es": "Sint Maarten", "pl": "Sint Maarten", "tr": "Sint Maarten", "ro": "Sint-Maarten", "it": "Sint Maarten", "cs": "Svatý Martin (nizozemská část)"}),
    ("syria",                       "963",  {"pt": "Síria", "en": "Syria", "es": "Siria", "pl": "Syria", "tr": "Suriye", "ro": "Siria", "it": "Siria", "cs": "Sýrie"}),
    ("somalia",                     "252",  {"pt": "Somália", "en": "Somalia", "es": "Somalia", "pl": "Somalia", "tr": "Somali", "ro": "Somalia", "it": "Somalia", "cs": "Somálsko"}),
    ("sri_lanka",                   "94",   {"pt": "Sri Lanka", "en": "Sri Lanka", "es": "Sri Lanka", "pl": "Sri Lanka", "tr": "Sri Lanka", "ro": "Sri Lanka", "it": "Sri Lanka", "cs": "Srí Lanka"}),
    ("sudan",                       "249",  {"pt": "Sudão", "en": "Sudan", "es": "Sudán", "pl": "Sudan", "tr": "Sudan", "ro": "Sudan", "it": "Sudan", "cs": "Súdán"}),
    ("south_sudan",                 "211",  {"pt": "Sudão do Sul", "en": "South Sudan", "es": "Sudán del Sur", "pl": "Sudan Południowy", "tr": "Güney Sudan", "ro": "Sudanul de Sud", "it": "Sud Sudan", "cs": "Jižní Súdán"}),
    ("sweden",                      "46",   {"pt": "Suécia", "en": "Sweden", "es": "Suecia", "pl": "Szwecja", "tr": "İsveç", "ro": "Suedia", "it": "Svezia", "cs": "Švédsko"}),
    ("switzerland",                 "41",   {"pt": "Suíça", "en": "Switzerland", "es": "Suiza", "pl": "Szwajcaria", "tr": "İsviçre", "ro": "Elveția", "it": "Svizzera", "cs": "Švýcarsko"}),
    ("suriname",                    "597",  {"pt": "Suriname", "en": "Suriname", "es": "Surinam", "pl": "Surinam", "tr": "Surinam", "ro": "Suriname", "it": "Suriname", "cs": "Surinam"}),

    # ── T ──────────────────────────────────────────────────────────────────────
    ("tajikistan",                  "992",  {"pt": "Tadjiquistão", "en": "Tajikistan", "es": "Tayikistán", "pl": "Tadżykistan", "tr": "Tacikistan", "ro": "Tadjikistan", "it": "Tagikistan", "cs": "Tádžikistán"}),
    ("thailand",                    "66",   {"pt": "Tailândia", "en": "Thailand", "es": "Tailandia", "pl": "Tajlandia", "tr": "Tayland", "ro": "Thailanda", "it": "Thailandia", "cs": "Thajsko"}),
    ("taiwan",                      "886",  {"pt": "Taiwan", "en": "Taiwan", "es": "Taiwán", "pl": "Tajwan", "tr": "Tayvan", "ro": "Taiwan", "it": "Taiwan", "cs": "Tchaj-wan"}),
    ("tanzania",                    "255",  {"pt": "Tanzânia", "en": "Tanzania", "es": "Tanzania", "pl": "Tanzania", "tr": "Tanzanya", "ro": "Tanzania", "it": "Tanzania", "cs": "Tanzanie"}),
    ("british_indian_ocean_territory","246",  {"pt": "Território Britânico do Oceano Índico", "en": "British Indian Ocean Territory", "es": "Territorio Británico del Océano Índico", "pl": "Brytyjskie Terytorium Oceanu Indyjskiego", "tr": "Britanya Hint Okyanusu Toprakları", "ro": "Teritoriul Britanic din Oceanul Indian", "it": "Territorio britannico dell’Oceano Indiano", "cs": "Britské indickooceánské území"}),
    ("palestine",                   "970",  {"pt": "Territórios palestinos", "en": "Palestinian Territories", "es": "Territorios Palestinos", "pl": "Terytoria Palestyńskie", "tr": "Filistin Bölgeleri", "ro": "Teritoriile Palestiniene", "it": "Territori Palestinesi", "cs": "Palestinská území"}),
    ("timor_leste",                 "670",  {"pt": "Timor-Leste", "en": "Timor-Leste", "es": "Timor Oriental", "pl": "Timor Wschodni", "tr": "Timor-Leste", "ro": "Timor-Leste", "it": "Timor Est", "cs": "Východní Timor"}),
    ("togo",                        "228",  {"pt": "Togo", "en": "Togo", "es": "Togo", "pl": "Togo", "tr": "Togo", "ro": "Togo", "it": "Togo", "cs": "Togo"}),
    ("tokelau",                     "690",  {"pt": "Tokelau", "en": "Tokelau", "es": "Tokelau", "pl": "Tokelau", "tr": "Tokelau", "ro": "Tokelau", "it": "Tokelau", "cs": "Tokelau"}),
    ("tonga",                       "676",  {"pt": "Tonga", "en": "Tonga", "es": "Tonga", "pl": "Tonga", "tr": "Tonga", "ro": "Tonga", "it": "Tonga", "cs": "Tonga"}),
    ("trinidad_and_tobago",         "1868", {"pt": "Trinidad e Tobago", "en": "Trinidad and Tobago", "es": "Trinidad y Tobago", "pl": "Trynidad i Tobago", "tr": "Trinidad ve Tobago", "ro": "Trinidad și Tobago", "it": "Trinidad e Tobago", "cs": "Trinidad a Tobago"}),
    ("tunisia",                     "216",  {"pt": "Tunísia", "en": "Tunisia", "es": "Túnez", "pl": "Tunezja", "tr": "Tunus", "ro": "Tunisia", "it": "Tunisia", "cs": "Tunisko"}),
    ("turkmenistan",                "993",  {"pt": "Turcomenistão", "en": "Turkmenistan", "es": "Turkmenistán", "pl": "Turkmenistan", "tr": "Türkmenistan", "ro": "Turkmenistan", "it": "Turkmenistan", "cs": "Turkmenistán"}),
    ("turkey",                      "90",   {"pt": "Turquia", "en": "Turkey", "es": "Turquía", "pl": "Turcja", "tr": "Türkiye", "ro": "Turcia", "it": "Turchia", "cs": "Turecko"}),
    ("tuvalu",                      "688",  {"pt": "Tuvalu", "en": "Tuvalu", "es": "Tuvalu", "pl": "Tuvalu", "tr": "Tuvalu", "ro": "Tuvalu", "it": "Tuvalu", "cs": "Tuvalu"}),

    # ── U ──────────────────────────────────────────────────────────────────────
    ("ukraine",                     "380",  {"pt": "Ucrânia", "en": "Ukraine", "es": "Ucrania", "pl": "Ukraina", "tr": "Ukrayna", "ro": "Ucraina", "it": "Ucraina", "cs": "Ukrajina"}),
    ("uganda",                      "256",  {"pt": "Uganda", "en": "Uganda", "es": "Uganda", "pl": "Uganda", "tr": "Uganda", "ro": "Uganda", "it": "Uganda", "cs": "Uganda"}),
    ("uruguay",                     "598",  {"pt": "Uruguai", "en": "Uruguay", "es": "Uruguay", "pl": "Urugwaj", "tr": "Uruguay", "ro": "Uruguay", "it": "Uruguay", "cs": "Uruguay"}),
    ("uzbekistan",                  "998",  {"pt": "Uzbequistão", "en": "Uzbekistan", "es": "Uzbekistán", "pl": "Uzbekistan", "tr": "Özbekistan", "ro": "Uzbekistan", "it": "Uzbekistan", "cs": "Uzbekistán"}),

    # ── V ──────────────────────────────────────────────────────────────────────
    ("vanuatu",                     "678",  {"pt": "Vanuatu", "en": "Vanuatu", "es": "Vanuatu", "pl": "Vanuatu", "tr": "Vanuatu", "ro": "Vanuatu", "it": "Vanuatu", "cs": "Vanuatu"}),
    ("venezuela",                   "58",   {"pt": "Venezuela", "en": "Venezuela", "es": "Venezuela", "pl": "Wenezuela", "tr": "Venezuela", "ro": "Venezuela", "it": "Venezuela", "cs": "Venezuela"}),
    ("vietnam",                     "84",   {"pt": "Vietnã", "en": "Vietnam", "es": "Vietnam", "pl": "Wietnam", "tr": "Vietnam", "ro": "Vietnam", "it": "Vietnam", "cs": "Vietnam"}),

    # ── W ──────────────────────────────────────────────────────────────────────
    ("wallis_and_futuna",           "681",  {"pt": "Wallis e Futuna", "en": "Wallis & Futuna", "es": "Wallis y Futuna", "pl": "Wallis i Futuna", "tr": "Wallis ve Futuna", "ro": "Wallis și Futuna", "it": "Wallis e Futuna", "cs": "Wallis a Futuna"}),

    # ── Z ──────────────────────────────────────────────────────────────────────
    ("zambia",                      "260",  {"pt": "Zâmbia", "en": "Zambia", "es": "Zambia", "pl": "Zambia", "tr": "Zambiya", "ro": "Zambia", "it": "Zambia", "cs": "Zambie"}),
    ("zimbabwe",                    "263",  {"pt": "Zimbábue", "en": "Zimbabwe", "es": "Zimbabue", "pl": "Zimbabwe", "tr": "Zimbabve", "ro": "Zimbabwe", "it": "Zimbabwe", "cs": "Zimbabwe"}),
]


# ISO 3166-1 alpha-2 code for every entry in _COUNTRIES, keyed by the same
# stable key — used only to match the Windows "Country or region" setting
# (core.locale_format.get_country_or_region_iso2(), an ISO 3166 alpha-2 code
# itself) back to one of our entries in get_default_country_index(). NOT the
# "Regional format" locale: reading that is the bug this whole path exists to
# fix, and the two Windows settings are independent — see that function's own
# docstring for the case that was reported live. Kept as
# a separate table rather than a 4th tuple field so the country data above
# stays exactly as easy to scan/edit as it was before this existed.
_KEY_TO_ISO2: dict[str, str] = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ",
    "american_samoa": "AS", "andorra": "AD", "angola": "AO",
    "anguilla": "AI", "antigua_and_barbuda": "AG", "argentina": "AR",
    "armenia": "AM", "aruba": "AW", "ascension_island": "AC",
    "australia": "AU", "austria": "AT", "azerbaijan": "AZ",
    "bahamas": "BS", "bahrain": "BH", "bangladesh": "BD", "barbados": "BB",
    "belarus": "BY", "belgium": "BE", "belize": "BZ", "benin": "BJ",
    "bermuda": "BM", "bhutan": "BT", "bolivia": "BO",
    "bosnia_and_herzegovina": "BA", "botswana": "BW", "brazil": "BR",
    "british_indian_ocean_territory": "IO", "british_virgin_islands": "VG",
    "brunei": "BN", "bulgaria": "BG", "burkina_faso": "BF",
    "burundi": "BI", "cambodia": "KH", "cameroon": "CM", "canada": "CA",
    "cape_verde": "CV", "caribbean_netherlands": "BQ",
    "cayman_islands": "KY", "central_african_republic": "CF", "chad": "TD",
    "chile": "CL", "china": "CN", "colombia": "CO", "comoros": "KM",
    "congo": "CG", "cook_islands": "CK", "costa_rica": "CR",
    "croatia": "HR", "cuba": "CU", "curacao": "CW", "cyprus": "CY",
    "czech_republic": "CZ", "democratic_republic_congo": "CD",
    "denmark": "DK", "djibouti": "DJ", "dominica": "DM",
    "dominican_republic": "DO", "ecuador": "EC", "egypt": "EG",
    "el_salvador": "SV", "equatorial_guinea": "GQ", "eritrea": "ER",
    "estonia": "EE", "eswatini": "SZ", "ethiopia": "ET",
    "falkland_islands": "FK", "faroe_islands": "FO", "fiji": "FJ",
    "finland": "FI", "france": "FR", "french_guiana": "GF",
    "french_polynesia": "PF", "gabon": "GA", "gambia": "GM",
    "georgia": "GE", "germany": "DE", "ghana": "GH", "gibraltar": "GI",
    "greece": "GR", "greenland": "GL", "grenada": "GD", "guadeloupe": "GP",
    "guam": "GU", "guatemala": "GT", "guernsey": "GG", "guinea": "GN",
    "guinea_bissau": "GW", "guyana": "GY", "haiti": "HT", "honduras": "HN",
    "hong_kong": "HK", "hungary": "HU", "iceland": "IS", "india": "IN",
    "indonesia": "ID", "iran": "IR", "iraq": "IQ", "ireland": "IE",
    "isle_of_man": "IM", "israel": "IL", "italy": "IT",
    "ivory_coast": "CI", "jamaica": "JM", "japan": "JP", "jersey": "JE",
    "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE", "kiribati": "KI",
    "kosovo": "XK", "kuwait": "KW", "kyrgyzstan": "KG", "laos": "LA",
    "latvia": "LV", "lebanon": "LB", "lesotho": "LS", "liberia": "LR",
    "libya": "LY", "liechtenstein": "LI", "lithuania": "LT",
    "luxembourg": "LU", "macau": "MO", "madagascar": "MG", "malawi": "MW",
    "malaysia": "MY", "maldives": "MV", "mali": "ML", "malta": "MT",
    "marshall_islands": "MH", "martinique": "MQ", "mauritania": "MR",
    "mauritius": "MU", "mayotte": "YT", "mexico": "MX", "micronesia": "FM",
    "moldova": "MD", "monaco": "MC", "mongolia": "MN", "montenegro": "ME",
    "montserrat": "MS", "morocco": "MA", "mozambique": "MZ",
    "myanmar": "MM", "namibia": "NA", "nauru": "NR", "nepal": "NP",
    "netherlands": "NL", "new_caledonia": "NC", "new_zealand": "NZ",
    "nicaragua": "NI", "niger": "NE", "nigeria": "NG", "niue": "NU",
    "norfolk_island": "NF", "north_korea": "KP", "north_macedonia": "MK",
    "northern_mariana_islands": "MP", "norway": "NO", "oman": "OM",
    "pakistan": "PK", "palau": "PW", "palestine": "PS", "panama": "PA",
    "papua_new_guinea": "PG", "paraguay": "PY", "peru": "PE",
    "philippines": "PH", "poland": "PL", "portugal": "PT",
    "puerto_rico": "PR", "qatar": "QA", "reunion": "RE", "romania": "RO",
    "russia": "RU", "rwanda": "RW", "saint_barthelemy": "BL",
    "saint_helena": "SH", "saint_kitts_and_nevis": "KN",
    "saint_lucia": "LC", "saint_martin": "MF",
    "saint_pierre_and_miquelon": "PM", "saint_vincent_grenadines": "VC",
    "samoa": "WS", "san_marino": "SM", "sao_tome_and_principe": "ST",
    "saudi_arabia": "SA", "senegal": "SN", "serbia": "RS",
    "seychelles": "SC", "sierra_leone": "SL", "singapore": "SG",
    "sint_maarten": "SX", "slovakia": "SK", "slovenia": "SI",
    "solomon_islands": "SB", "somalia": "SO", "south_africa": "ZA",
    "south_korea": "KR", "south_sudan": "SS", "spain": "ES",
    "sri_lanka": "LK", "sudan": "SD", "suriname": "SR", "sweden": "SE",
    "switzerland": "CH", "syria": "SY", "taiwan": "TW", "tajikistan": "TJ",
    "tanzania": "TZ", "thailand": "TH", "timor_leste": "TL", "togo": "TG",
    "tokelau": "TK", "tonga": "TO", "trinidad_and_tobago": "TT",
    "tunisia": "TN", "turkey": "TR", "turkmenistan": "TM",
    "turks_and_caicos_islands": "TC", "tuvalu": "TV", "uganda": "UG",
    "ukraine": "UA", "united_arab_emirates": "AE", "united_kingdom": "GB",
    "united_states": "US", "uruguay": "UY", "us_virgin_islands": "VI",
    "uzbekistan": "UZ", "vanuatu": "VU", "vatican_city": "VA",
    "venezuela": "VE", "vietnam": "VN", "wallis_and_futuna": "WF",
    "yemen": "YE", "zambia": "ZM", "zimbabwe": "ZW",
}
_ISO2_TO_KEY: dict[str, str] = {v: k for k, v in _KEY_TO_ISO2.items()}


def _sort_key(name: str) -> str:
    """Diacritic-insensitive, case-insensitive sort key so e.g. "Áustria"
    sorts next to "Austrália" instead of after every plain ASCII letter.
    Reuses core.utils.normalize_for_search()'s "nfkd" mode (lowercase,
    NFKD-decompose, drop combining marks) rather than duplicating that
    logic here."""
    return normalize_for_search(name, mode="nfkd")


def _localized_name(names: dict[str, str], lang_key: str) -> str:
    return names.get(lang_key) or names["pt"]


def get_countries(lang: str = "pt-BR") -> list[tuple[str, str]]:
    """Return [(display_name, dial_code), ...] localized for *lang* and
    sorted alphabetically (start to end, diacritics ignored) by that
    localized name. Falls back to Portuguese for any unrecognized lang code.

    The order is intentionally NOT stable across languages — display names
    differ per language, so alphabetical order does too — and no entry is
    pinned first. Callers must index back into the SAME list this returned,
    never assume a fixed position for a given country."""
    key = _LANG_ALIASES.get(lang, "pt")
    entries = [
        (_localized_name(names, key), code) for _, code, names in _COUNTRIES
    ]
    entries.sort(key=lambda item: _sort_key(item[0]))
    return [(f"{name} (+{code})", code) for name, code in entries]


def get_default_country_index(countries: list[tuple[str, str]], lang: str = "pt-BR") -> int:
    """Index into *countries* (as returned by get_countries(lang)) of the
    country matching the user's Windows "Country or region" setting, or of
    the United States if that can't be detected or isn't one of our
    entries. Falls back to index 0 if even the United States entry can't be
    found (should not happen — it's always present in _COUNTRIES).

    Deliberately based on Country or region (a location, independent of any
    language setting) rather than the UI/display language or the "Regional
    format" locale — see core.locale_format.get_country_or_region_iso2()'s
    own docstring for why those would give the wrong answer here."""
    # Imported here, not at module load, so this module (and get_countries())
    # stays importable/testable without pulling in ctypes/Windows-only code.
    from core.locale_format import get_country_or_region_iso2

    iso2 = (get_country_or_region_iso2() or "").upper()
    stable_key = _ISO2_TO_KEY.get(iso2, "united_states")
    lang_key = _LANG_ALIASES.get(lang, "pt")

    target = None
    for key, code, names in _COUNTRIES:
        if key == stable_key:
            target = (f"{_localized_name(names, lang_key)} (+{code})", code)
            break
    if target is None or target not in countries:
        # Detected country isn't one of ours (or detection failed) — fall
        # back to the United States entry.
        for key, code, names in _COUNTRIES:
            if key == "united_states":
                target = (f"{_localized_name(names, lang_key)} (+{code})", code)
                break

    if target is not None:
        try:
            return countries.index(target)
        except ValueError:
            pass
    return 0


# Backward-compat: static pt-BR list for code that only needs the dial codes
# (e.g. core/utils.py's country-code matching), not the localized names.
COUNTRIES: list[tuple[str, str]] = get_countries("pt-BR")
