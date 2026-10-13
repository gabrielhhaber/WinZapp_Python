"""
Lista de países suportados pelo WhatsApp com seus códigos de discagem,
localizada de acordo com o idioma da interface selecionado pelo usuário.

Cada entrada de _COUNTRIES é uma tupla (chave_estável, código_discagem, nomes):
  • chave_estável: identificador interno, nunca muda (independe de idioma).
  • código_discagem: apenas dígitos, sem o "+".
  • nomes: dict {"pt", "en", "es", "pl", "tr", "ro"} com o nome do país em
    cada idioma da interface (português vale para pt-BR e pt-PT). Todo país
    deve ter os seis idiomas: tests/test_countries.py garante.

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
}

_COUNTRIES: list[tuple[str, str, dict[str, str]]] = [
    # ── Padrão ──────────────────────────────────────────────────────────────
    ("brazil",                      "55",   {"pt": "Brasil", "en": "Brazil", "es": "Brasil", "pl": "Brazylia", "tr": "Brezilya", "ro": "Brazilia"}),

    # ── A ──────────────────────────────────────────────────────────────────────
    ("afghanistan",                 "93",   {"pt": "Afeganistão", "en": "Afghanistan", "es": "Afganistán", "pl": "Afganistan", "tr": "Afganistan", "ro": "Afganistan"}),
    ("south_africa",                "27",   {"pt": "África do Sul", "en": "South Africa", "es": "Sudáfrica", "pl": "Republika Południowej Afryki", "tr": "Güney Afrika", "ro": "Africa de Sud"}),
    ("albania",                     "355",  {"pt": "Albânia", "en": "Albania", "es": "Albania", "pl": "Albania", "tr": "Arnavutluk", "ro": "Albania"}),
    ("germany",                     "49",   {"pt": "Alemanha", "en": "Germany", "es": "Alemania", "pl": "Niemcy", "tr": "Almanya", "ro": "Germania"}),
    ("andorra",                     "376",  {"pt": "Andorra", "en": "Andorra", "es": "Andorra", "pl": "Andora", "tr": "Andorra", "ro": "Andorra"}),
    ("angola",                      "244",  {"pt": "Angola", "en": "Angola", "es": "Angola", "pl": "Angola", "tr": "Angola", "ro": "Angola"}),
    ("anguilla",                    "1264", {"pt": "Anguila", "en": "Anguilla", "es": "Anguila", "pl": "Anguilla", "tr": "Anguilla", "ro": "Anguilla"}),
    ("antigua_and_barbuda",         "1268", {"pt": "Antígua e Barbuda", "en": "Antigua and Barbuda", "es": "Antigua y Barbuda", "pl": "Antigua i Barbuda", "tr": "Antigua ve Barbuda", "ro": "Antigua și Barbuda"}),
    ("saudi_arabia",                "966",  {"pt": "Arábia Saudita", "en": "Saudi Arabia", "es": "Arabia Saudita", "pl": "Arabia Saudyjska", "tr": "Suudi Arabistan", "ro": "Arabia Saudită"}),
    ("algeria",                     "213",  {"pt": "Argélia", "en": "Algeria", "es": "Argelia", "pl": "Algieria", "tr": "Cezayir", "ro": "Algeria"}),
    ("argentina",                   "54",   {"pt": "Argentina", "en": "Argentina", "es": "Argentina", "pl": "Argentyna", "tr": "Arjantin", "ro": "Argentina"}),
    ("armenia",                     "374",  {"pt": "Armênia", "en": "Armenia", "es": "Armenia", "pl": "Armenia", "tr": "Ermenistan", "ro": "Armenia"}),
    ("aruba",                       "297",  {"pt": "Aruba", "en": "Aruba", "es": "Aruba", "pl": "Aruba", "tr": "Aruba", "ro": "Aruba"}),
    ("australia",                   "61",   {"pt": "Austrália", "en": "Australia", "es": "Australia", "pl": "Australia", "tr": "Avustralya", "ro": "Australia"}),
    ("austria",                     "43",   {"pt": "Áustria", "en": "Austria", "es": "Austria", "pl": "Austria", "tr": "Avusturya", "ro": "Austria"}),
    ("azerbaijan",                  "994",  {"pt": "Azerbaijão", "en": "Azerbaijan", "es": "Azerbaiyán", "pl": "Azerbejdżan", "tr": "Azerbaycan", "ro": "Azerbaidjan"}),

    # ── B ──────────────────────────────────────────────────────────────────────
    ("bahamas",                     "1242", {"pt": "Bahamas", "en": "Bahamas", "es": "Bahamas", "pl": "Bahamy", "tr": "Bahamalar", "ro": "Bahamas"}),
    ("bangladesh",                  "880",  {"pt": "Bangladesh", "en": "Bangladesh", "es": "Bangladés", "pl": "Bangladesz", "tr": "Bangladeş", "ro": "Bangladesh"}),
    ("barbados",                    "1246", {"pt": "Barbados", "en": "Barbados", "es": "Barbados", "pl": "Barbados", "tr": "Barbados", "ro": "Barbados"}),
    ("bahrain",                     "973",  {"pt": "Barein", "en": "Bahrain", "es": "Baréin", "pl": "Bahrajn", "tr": "Bahreyn", "ro": "Bahrain"}),
    ("belgium",                     "32",   {"pt": "Bélgica", "en": "Belgium", "es": "Bélgica", "pl": "Belgia", "tr": "Belçika", "ro": "Belgia"}),
    ("belize",                      "501",  {"pt": "Belize", "en": "Belize", "es": "Belice", "pl": "Belize", "tr": "Belize", "ro": "Belize"}),
    ("benin",                       "229",  {"pt": "Benim", "en": "Benin", "es": "Benín", "pl": "Benin", "tr": "Benin", "ro": "Benin"}),
    ("bermuda",                     "1441", {"pt": "Bermudas", "en": "Bermuda", "es": "Bermudas", "pl": "Bermudy", "tr": "Bermuda", "ro": "Bermuda"}),
    ("belarus",                     "375",  {"pt": "Bielorrússia", "en": "Belarus", "es": "Bielorrusia", "pl": "Białoruś", "tr": "Belarus", "ro": "Belarus"}),
    ("bolivia",                     "591",  {"pt": "Bolívia", "en": "Bolivia", "es": "Bolivia", "pl": "Boliwia", "tr": "Bolivya", "ro": "Bolivia"}),
    ("bosnia_and_herzegovina",      "387",  {"pt": "Bósnia e Herzegovina", "en": "Bosnia and Herzegovina", "es": "Bosnia y Herzegovina", "pl": "Bośnia i Hercegowina", "tr": "Bosna-Hersek", "ro": "Bosnia și Herțegovina"}),
    ("botswana",                    "267",  {"pt": "Botsuana", "en": "Botswana", "es": "Botsuana", "pl": "Botswana", "tr": "Botsvana", "ro": "Botswana"}),
    ("brunei",                      "673",  {"pt": "Brunei", "en": "Brunei", "es": "Brunéi", "pl": "Brunei", "tr": "Brunei", "ro": "Brunei"}),
    ("bulgaria",                    "359",  {"pt": "Bulgária", "en": "Bulgaria", "es": "Bulgaria", "pl": "Bułgaria", "tr": "Bulgaristan", "ro": "Bulgaria"}),
    ("burkina_faso",                "226",  {"pt": "Burkina Faso", "en": "Burkina Faso", "es": "Burkina Faso", "pl": "Burkina Faso", "tr": "Burkina Faso", "ro": "Burkina Faso"}),
    ("burundi",                     "257",  {"pt": "Burundi", "en": "Burundi", "es": "Burundi", "pl": "Burundi", "tr": "Burundi", "ro": "Burundi"}),
    ("bhutan",                      "975",  {"pt": "Butão", "en": "Bhutan", "es": "Bután", "pl": "Bhutan", "tr": "Butan", "ro": "Bhutan"}),

    # ── C ──────────────────────────────────────────────────────────────────────
    ("cape_verde",                  "238",  {"pt": "Cabo Verde", "en": "Cape Verde", "es": "Cabo Verde", "pl": "Republika Zielonego Przylądka", "tr": "Cabo Verde", "ro": "Capul Verde"}),
    ("cameroon",                    "237",  {"pt": "Camarões", "en": "Cameroon", "es": "Camerún", "pl": "Kamerun", "tr": "Kamerun", "ro": "Camerun"}),
    ("cambodia",                    "855",  {"pt": "Camboja", "en": "Cambodia", "es": "Camboya", "pl": "Kambodża", "tr": "Kamboçya", "ro": "Cambodgia"}),
    ("canada",                      "1",    {"pt": "Canadá", "en": "Canada", "es": "Canadá", "pl": "Kanada", "tr": "Kanada", "ro": "Canada"}),
    ("qatar",                       "974",  {"pt": "Catar", "en": "Qatar", "es": "Catar", "pl": "Katar", "tr": "Katar", "ro": "Qatar"}),
    ("kazakhstan",                  "7",    {"pt": "Cazaquistão", "en": "Kazakhstan", "es": "Kazajistán", "pl": "Kazachstan", "tr": "Kazakistan", "ro": "Kazahstan"}),
    ("chad",                        "235",  {"pt": "Chade", "en": "Chad", "es": "Chad", "pl": "Czad", "tr": "Çad", "ro": "Ciad"}),
    ("chile",                       "56",   {"pt": "Chile", "en": "Chile", "es": "Chile", "pl": "Chile", "tr": "Şili", "ro": "Chile"}),
    ("china",                       "86",   {"pt": "China", "en": "China", "es": "China", "pl": "Chiny", "tr": "Çin", "ro": "China"}),
    ("cyprus",                      "357",  {"pt": "Chipre", "en": "Cyprus", "es": "Chipre", "pl": "Cypr", "tr": "Kıbrıs", "ro": "Cipru"}),
    ("vatican_city",                "39",   {"pt": "Cidade do Vaticano", "en": "Vatican City", "es": "Ciudad del Vaticano", "pl": "Watykan", "tr": "Vatikan", "ro": "Statul Cetății Vaticanului"}),
    ("colombia",                    "57",   {"pt": "Colômbia", "en": "Colombia", "es": "Colombia", "pl": "Kolumbia", "tr": "Kolombiya", "ro": "Columbia"}),
    ("comoros",                     "269",  {"pt": "Comores", "en": "Comoros", "es": "Comoras", "pl": "Komory", "tr": "Komorlar", "ro": "Comore"}),
    ("congo",                       "242",  {"pt": "Congo", "en": "Congo", "es": "Congo", "pl": "Kongo", "tr": "Kongo - Brazavil", "ro": "Congo - Brazzaville"}),
    ("north_korea",                 "850",  {"pt": "Coreia do Norte", "en": "North Korea", "es": "Corea del Norte", "pl": "Korea Północna", "tr": "Kuzey Kore", "ro": "Coreea de Nord"}),
    ("south_korea",                 "82",   {"pt": "Coreia do Sul", "en": "South Korea", "es": "Corea del Sur", "pl": "Korea Południowa", "tr": "Güney Kore", "ro": "Coreea de Sud"}),
    ("ivory_coast",                 "225",  {"pt": "Costa do Marfim", "en": "Ivory Coast", "es": "Costa de Marfil", "pl": "Côte d’Ivoire", "tr": "Côte d’Ivoire", "ro": "Côte d’Ivoire"}),
    ("costa_rica",                  "506",  {"pt": "Costa Rica", "en": "Costa Rica", "es": "Costa Rica", "pl": "Kostaryka", "tr": "Kosta Rika", "ro": "Costa Rica"}),
    ("croatia",                     "385",  {"pt": "Croácia", "en": "Croatia", "es": "Croacia", "pl": "Chorwacja", "tr": "Hırvatistan", "ro": "Croația"}),
    ("cuba",                        "53",   {"pt": "Cuba", "en": "Cuba", "es": "Cuba", "pl": "Kuba", "tr": "Küba", "ro": "Cuba"}),
    ("curacao",                     "5999", {"pt": "Curaçao", "en": "Curaçao", "es": "Curazao", "pl": "Curaçao", "tr": "Curaçao", "ro": "Curaçao"}),

    # ── D ──────────────────────────────────────────────────────────────────────
    ("denmark",                     "45",   {"pt": "Dinamarca", "en": "Denmark", "es": "Dinamarca", "pl": "Dania", "tr": "Danimarka", "ro": "Danemarca"}),
    ("djibouti",                    "253",  {"pt": "Djibuti", "en": "Djibouti", "es": "Yibuti", "pl": "Dżibuti", "tr": "Cibuti", "ro": "Djibouti"}),
    ("dominica",                    "1767", {"pt": "Dominica", "en": "Dominica", "es": "Dominica", "pl": "Dominika", "tr": "Dominika", "ro": "Dominica"}),

    # ── E ──────────────────────────────────────────────────────────────────────
    ("egypt",                       "20",   {"pt": "Egito", "en": "Egypt", "es": "Egipto", "pl": "Egipt", "tr": "Mısır", "ro": "Egipt"}),
    ("el_salvador",                 "503",  {"pt": "El Salvador", "en": "El Salvador", "es": "El Salvador", "pl": "Salwador", "tr": "El Salvador", "ro": "El Salvador"}),
    ("united_arab_emirates",        "971",  {"pt": "Emirados Árabes Unidos", "en": "United Arab Emirates", "es": "Emiratos Árabes Unidos", "pl": "Zjednoczone Emiraty Arabskie", "tr": "Birleşik Arap Emirlikleri", "ro": "Emiratele Arabe Unite"}),
    ("ecuador",                     "593",  {"pt": "Equador", "en": "Ecuador", "es": "Ecuador", "pl": "Ekwador", "tr": "Ekvador", "ro": "Ecuador"}),
    ("eritrea",                     "291",  {"pt": "Eritreia", "en": "Eritrea", "es": "Eritrea", "pl": "Erytrea", "tr": "Eritre", "ro": "Eritreea"}),
    ("slovakia",                    "421",  {"pt": "Eslováquia", "en": "Slovakia", "es": "Eslovaquia", "pl": "Słowacja", "tr": "Slovakya", "ro": "Slovacia"}),
    ("slovenia",                    "386",  {"pt": "Eslovênia", "en": "Slovenia", "es": "Eslovenia", "pl": "Słowenia", "tr": "Slovenya", "ro": "Slovenia"}),
    ("spain",                       "34",   {"pt": "Espanha", "en": "Spain", "es": "España", "pl": "Hiszpania", "tr": "İspanya", "ro": "Spania"}),
    ("united_states",               "1",    {"pt": "Estados Unidos", "en": "United States", "es": "Estados Unidos", "pl": "Stany Zjednoczone", "tr": "Amerika Birleşik Devletleri", "ro": "Statele Unite ale Americii"}),
    ("estonia",                     "372",  {"pt": "Estônia", "en": "Estonia", "es": "Estonia", "pl": "Estonia", "tr": "Estonya", "ro": "Estonia"}),
    ("eswatini",                    "268",  {"pt": "Eswatini", "en": "Eswatini", "es": "Esuatini", "pl": "Eswatini", "tr": "Esvatini", "ro": "Eswatini"}),
    ("ethiopia",                    "251",  {"pt": "Etiópia", "en": "Ethiopia", "es": "Etiopía", "pl": "Etiopia", "tr": "Etiyopya", "ro": "Etiopia"}),

    # ── F ──────────────────────────────────────────────────────────────────────
    ("fiji",                        "679",  {"pt": "Fiji", "en": "Fiji", "es": "Fiyi", "pl": "Fidżi", "tr": "Fiji", "ro": "Fiji"}),
    ("philippines",                 "63",   {"pt": "Filipinas", "en": "Philippines", "es": "Filipinas", "pl": "Filipiny", "tr": "Filipinler", "ro": "Filipine"}),
    ("finland",                     "358",  {"pt": "Finlândia", "en": "Finland", "es": "Finlandia", "pl": "Finlandia", "tr": "Finlandiya", "ro": "Finlanda"}),
    ("france",                      "33",   {"pt": "França", "en": "France", "es": "Francia", "pl": "Francja", "tr": "Fransa", "ro": "Franța"}),

    # ── G ──────────────────────────────────────────────────────────────────────
    ("gabon",                       "241",  {"pt": "Gabão", "en": "Gabon", "es": "Gabón", "pl": "Gabon", "tr": "Gabon", "ro": "Gabon"}),
    ("gambia",                      "220",  {"pt": "Gâmbia", "en": "Gambia", "es": "Gambia", "pl": "Gambia", "tr": "Gambiya", "ro": "Gambia"}),
    ("ghana",                       "233",  {"pt": "Gana", "en": "Ghana", "es": "Ghana", "pl": "Ghana", "tr": "Gana", "ro": "Ghana"}),
    ("georgia",                     "995",  {"pt": "Geórgia", "en": "Georgia", "es": "Georgia", "pl": "Gruzja", "tr": "Gürcistan", "ro": "Georgia"}),
    ("gibraltar",                   "350",  {"pt": "Gibraltar", "en": "Gibraltar", "es": "Gibraltar", "pl": "Gibraltar", "tr": "Cebelitarık", "ro": "Gibraltar"}),
    ("grenada",                     "1473", {"pt": "Granada", "en": "Grenada", "es": "Granada", "pl": "Grenada", "tr": "Grenada", "ro": "Grenada"}),
    ("greece",                      "30",   {"pt": "Grécia", "en": "Greece", "es": "Grecia", "pl": "Grecja", "tr": "Yunanistan", "ro": "Grecia"}),
    ("greenland",                   "299",  {"pt": "Groenlândia", "en": "Greenland", "es": "Groenlandia", "pl": "Grenlandia", "tr": "Grönland", "ro": "Groenlanda"}),
    ("guadeloupe",                  "590",  {"pt": "Guadalupe", "en": "Guadeloupe", "es": "Guadalupe", "pl": "Gwadelupa", "tr": "Guadeloupe", "ro": "Guadelupa"}),
    ("guam",                        "1671", {"pt": "Guam", "en": "Guam", "es": "Guam", "pl": "Guam", "tr": "Guam", "ro": "Guam"}),
    ("guatemala",                   "502",  {"pt": "Guatemala", "en": "Guatemala", "es": "Guatemala", "pl": "Gwatemala", "tr": "Guatemala", "ro": "Guatemala"}),
    ("guernsey",                    "44",   {"pt": "Guernsey", "en": "Guernsey", "es": "Guernesey", "pl": "Guernsey", "tr": "Guernsey", "ro": "Guernsey"}),
    ("guyana",                      "592",  {"pt": "Guiana", "en": "Guyana", "es": "Guyana", "pl": "Gujana", "tr": "Guyana", "ro": "Guyana"}),
    ("french_guiana",               "594",  {"pt": "Guiana Francesa", "en": "French Guiana", "es": "Guayana Francesa", "pl": "Gujana Francuska", "tr": "Fransız Guyanası", "ro": "Guyana Franceză"}),
    ("guinea",                      "224",  {"pt": "Guiné", "en": "Guinea", "es": "Guinea", "pl": "Gwinea", "tr": "Gine", "ro": "Guineea"}),
    ("guinea_bissau",               "245",  {"pt": "Guiné Bissau", "en": "Guinea-Bissau", "es": "Guinea-Bisáu", "pl": "Gwinea Bissau", "tr": "Gine-Bissau", "ro": "Guineea-Bissau"}),
    ("equatorial_guinea",           "240",  {"pt": "Guiné Equatorial", "en": "Equatorial Guinea", "es": "Guinea Ecuatorial", "pl": "Gwinea Równikowa", "tr": "Ekvator Ginesi", "ro": "Guineea Ecuatorială"}),

    # ── H ──────────────────────────────────────────────────────────────────────
    ("haiti",                       "509",  {"pt": "Haiti", "en": "Haiti", "es": "Haití", "pl": "Haiti", "tr": "Haiti", "ro": "Haiti"}),
    ("honduras",                    "504",  {"pt": "Honduras", "en": "Honduras", "es": "Honduras", "pl": "Honduras", "tr": "Honduras", "ro": "Honduras"}),
    ("hong_kong",                   "852",  {"pt": "Hong Kong", "en": "Hong Kong", "es": "Hong Kong", "pl": "SRA Hongkong (Chiny)", "tr": "Çin Hong Kong ÖİB", "ro": "R.A.S. Hong Kong, China"}),
    ("hungary",                     "36",   {"pt": "Hungria", "en": "Hungary", "es": "Hungría", "pl": "Węgry", "tr": "Macaristan", "ro": "Ungaria"}),

    # ── I ──────────────────────────────────────────────────────────────────────
    ("yemen",                       "967",  {"pt": "Iêmen", "en": "Yemen", "es": "Yemen", "pl": "Jemen", "tr": "Yemen", "ro": "Yemen"}),
    ("ascension_island",            "247",  {"pt": "Ilha de Ascensão", "en": "Ascension Island", "es": "Isla de la Ascensión", "pl": "Wyspa Wniebowstąpienia", "tr": "Ascension Adası", "ro": "Insula Ascension"}),
    ("isle_of_man",                 "44",   {"pt": "Ilha de Man", "en": "Isle of Man", "es": "Isla de Man", "pl": "Wyspa Man", "tr": "Man Adası", "ro": "Insula Man"}),
    ("norfolk_island",              "672",  {"pt": "Ilha Norfolk", "en": "Norfolk Island", "es": "Isla Norfolk", "pl": "Norfolk", "tr": "Norfolk Adası", "ro": "Insula Norfolk"}),
    ("cayman_islands",              "1345", {"pt": "Ilhas Cayman", "en": "Cayman Islands", "es": "Islas Caimán", "pl": "Kajmany", "tr": "Cayman Adaları", "ro": "Insulele Cayman"}),
    ("cook_islands",                "682",  {"pt": "Ilhas Cook", "en": "Cook Islands", "es": "Islas Cook", "pl": "Wyspy Cooka", "tr": "Cook Adaları", "ro": "Insulele Cook"}),
    ("faroe_islands",               "298",  {"pt": "Ilhas Faroe", "en": "Faroe Islands", "es": "Islas Feroe", "pl": "Wyspy Owcze", "tr": "Faroe Adaları", "ro": "Insulele Feroe"}),
    ("falkland_islands",            "500",  {"pt": "Ilhas Malvinas", "en": "Falkland Islands", "es": "Islas Malvinas", "pl": "Falklandy", "tr": "Falkland Adaları", "ro": "Insulele Falkland"}),
    ("northern_mariana_islands",    "1670", {"pt": "Ilhas Marianas do Norte", "en": "Northern Mariana Islands", "es": "Islas Marianas del Norte", "pl": "Mariany Północne", "tr": "Kuzey Mariana Adaları", "ro": "Insulele Mariane de Nord"}),
    ("marshall_islands",            "692",  {"pt": "Ilhas Marshall", "en": "Marshall Islands", "es": "Islas Marshall", "pl": "Wyspy Marshalla", "tr": "Marshall Adaları", "ro": "Insulele Marshall"}),
    ("solomon_islands",             "677",  {"pt": "Ilhas Salomão", "en": "Solomon Islands", "es": "Islas Salomón", "pl": "Wyspy Salomona", "tr": "Solomon Adaları", "ro": "Insulele Solomon"}),
    ("turks_and_caicos_islands",    "1649", {"pt": "Ilhas Turks e Caicos", "en": "Turks and Caicos Islands", "es": "Islas Turcas y Caicos", "pl": "Turks i Caicos", "tr": "Turks ve Caicos Adaları", "ro": "Insulele Turks și Caicos"}),
    ("british_virgin_islands",      "1284", {"pt": "Ilhas Virgens Britânicas", "en": "British Virgin Islands", "es": "Islas Vírgenes Británicas", "pl": "Brytyjskie Wyspy Dziewicze", "tr": "Britanya Virjin Adaları", "ro": "Insulele Virgine Britanice"}),
    ("us_virgin_islands",           "1340", {"pt": "Ilhas Virgens dos EUA", "en": "U.S. Virgin Islands", "es": "Islas Vírgenes de EE. UU.", "pl": "Wyspy Dziewicze Stanów Zjednoczonych", "tr": "ABD Virjin Adaları", "ro": "Insulele Virgine Americane"}),
    ("india",                       "91",   {"pt": "Índia", "en": "India", "es": "India", "pl": "Indie", "tr": "Hindistan", "ro": "India"}),
    ("indonesia",                   "62",   {"pt": "Indonésia", "en": "Indonesia", "es": "Indonesia", "pl": "Indonezja", "tr": "Endonezya", "ro": "Indonezia"}),
    ("iran",                        "98",   {"pt": "Irã", "en": "Iran", "es": "Irán", "pl": "Iran", "tr": "İran", "ro": "Iran"}),
    ("iraq",                        "964",  {"pt": "Iraque", "en": "Iraq", "es": "Irak", "pl": "Irak", "tr": "Irak", "ro": "Irak"}),
    ("ireland",                     "353",  {"pt": "Irlanda", "en": "Ireland", "es": "Irlanda", "pl": "Irlandia", "tr": "İrlanda", "ro": "Irlanda"}),
    ("iceland",                     "354",  {"pt": "Islândia", "en": "Iceland", "es": "Islandia", "pl": "Islandia", "tr": "İzlanda", "ro": "Islanda"}),
    ("israel",                      "972",  {"pt": "Israel", "en": "Israel", "es": "Israel", "pl": "Izrael", "tr": "İsrail", "ro": "Israel"}),
    ("italy",                       "39",   {"pt": "Itália", "en": "Italy", "es": "Italia", "pl": "Włochy", "tr": "İtalya", "ro": "Italia"}),

    # ── J ──────────────────────────────────────────────────────────────────────
    ("jamaica",                     "1876", {"pt": "Jamaica", "en": "Jamaica", "es": "Jamaica", "pl": "Jamajka", "tr": "Jamaika", "ro": "Jamaica"}),
    ("japan",                       "81",   {"pt": "Japão", "en": "Japan", "es": "Japón", "pl": "Japonia", "tr": "Japonya", "ro": "Japonia"}),
    ("jersey",                      "44",   {"pt": "Jersey", "en": "Jersey", "es": "Jersey", "pl": "Jersey", "tr": "Jersey", "ro": "Jersey"}),
    ("jordan",                      "962",  {"pt": "Jordânia", "en": "Jordan", "es": "Jordania", "pl": "Jordania", "tr": "Ürdün", "ro": "Iordania"}),

    # ── K ──────────────────────────────────────────────────────────────────────
    ("kosovo",                      "383",  {"pt": "Kosovo", "en": "Kosovo", "es": "Kosovo", "pl": "Kosowo", "tr": "Kosova", "ro": "Kosovo"}),
    ("kuwait",                      "965",  {"pt": "Kuwait", "en": "Kuwait", "es": "Kuwait", "pl": "Kuwejt", "tr": "Kuveyt", "ro": "Kuweit"}),

    # ── L ──────────────────────────────────────────────────────────────────────
    ("laos",                        "856",  {"pt": "Laos", "en": "Laos", "es": "Laos", "pl": "Laos", "tr": "Laos", "ro": "Laos"}),
    ("lesotho",                     "266",  {"pt": "Lesoto", "en": "Lesotho", "es": "Lesoto", "pl": "Lesotho", "tr": "Lesotho", "ro": "Lesotho"}),
    ("latvia",                      "371",  {"pt": "Letônia", "en": "Latvia", "es": "Letonia", "pl": "Łotwa", "tr": "Letonya", "ro": "Letonia"}),
    ("lebanon",                     "961",  {"pt": "Líbano", "en": "Lebanon", "es": "Líbano", "pl": "Liban", "tr": "Lübnan", "ro": "Liban"}),
    ("liberia",                     "231",  {"pt": "Libéria", "en": "Liberia", "es": "Liberia", "pl": "Liberia", "tr": "Liberya", "ro": "Liberia"}),
    ("libya",                       "218",  {"pt": "Líbia", "en": "Libya", "es": "Libia", "pl": "Libia", "tr": "Libya", "ro": "Libia"}),
    ("liechtenstein",               "423",  {"pt": "Liechtenstein", "en": "Liechtenstein", "es": "Liechtenstein", "pl": "Liechtenstein", "tr": "Liechtenstein", "ro": "Liechtenstein"}),
    ("lithuania",                   "370",  {"pt": "Lituânia", "en": "Lithuania", "es": "Lituania", "pl": "Litwa", "tr": "Litvanya", "ro": "Lituania"}),
    ("luxembourg",                  "352",  {"pt": "Luxemburgo", "en": "Luxembourg", "es": "Luxemburgo", "pl": "Luksemburg", "tr": "Lüksemburg", "ro": "Luxemburg"}),

    # ── M ──────────────────────────────────────────────────────────────────────
    ("macau",                       "853",  {"pt": "Macau", "en": "Macau", "es": "Macao", "pl": "SRA Makau (Chiny)", "tr": "Çin Makao ÖİB", "ro": "R.A.S. Macao, China"}),
    ("north_macedonia",             "389",  {"pt": "Macedônia do Norte", "en": "North Macedonia", "es": "Macedonia del Norte", "pl": "Macedonia Północna", "tr": "Kuzey Makedonya", "ro": "Macedonia de Nord"}),
    ("madagascar",                  "261",  {"pt": "Madagascar", "en": "Madagascar", "es": "Madagascar", "pl": "Madagaskar", "tr": "Madagaskar", "ro": "Madagascar"}),
    ("malaysia",                    "60",   {"pt": "Malásia", "en": "Malaysia", "es": "Malasia", "pl": "Malezja", "tr": "Malezya", "ro": "Malaysia"}),
    ("malawi",                      "265",  {"pt": "Malawi", "en": "Malawi", "es": "Malaui", "pl": "Malawi", "tr": "Malavi", "ro": "Malawi"}),
    ("maldives",                    "960",  {"pt": "Maldivas", "en": "Maldives", "es": "Maldivas", "pl": "Malediwy", "tr": "Maldivler", "ro": "Maldive"}),
    ("mali",                        "223",  {"pt": "Mali", "en": "Mali", "es": "Malí", "pl": "Mali", "tr": "Mali", "ro": "Mali"}),
    ("malta",                       "356",  {"pt": "Malta", "en": "Malta", "es": "Malta", "pl": "Malta", "tr": "Malta", "ro": "Malta"}),
    ("morocco",                     "212",  {"pt": "Marrocos", "en": "Morocco", "es": "Marruecos", "pl": "Maroko", "tr": "Fas", "ro": "Maroc"}),
    ("martinique",                  "596",  {"pt": "Martinica", "en": "Martinique", "es": "Martinica", "pl": "Martynika", "tr": "Martinik", "ro": "Martinica"}),
    ("mauritius",                   "230",  {"pt": "Maurício", "en": "Mauritius", "es": "Mauricio", "pl": "Mauritius", "tr": "Mauritius", "ro": "Mauritius"}),
    ("mauritania",                  "222",  {"pt": "Mauritânia", "en": "Mauritania", "es": "Mauritania", "pl": "Mauretania", "tr": "Moritanya", "ro": "Mauritania"}),
    ("mayotte",                     "262",  {"pt": "Mayotte", "en": "Mayotte", "es": "Mayotte", "pl": "Majotta", "tr": "Mayotte", "ro": "Mayotte"}),
    ("mexico",                      "52",   {"pt": "México", "en": "Mexico", "es": "México", "pl": "Meksyk", "tr": "Meksika", "ro": "Mexic"}),
    ("myanmar",                     "95",   {"pt": "Mianmar", "en": "Myanmar", "es": "Myanmar", "pl": "Mjanma (Birma)", "tr": "Myanmar (Burma)", "ro": "Myanmar (Birmania)"}),
    ("micronesia",                  "691",  {"pt": "Micronésia", "en": "Micronesia", "es": "Micronesia", "pl": "Mikronezja", "tr": "Mikronezya", "ro": "Micronezia"}),
    ("mozambique",                  "258",  {"pt": "Moçambique", "en": "Mozambique", "es": "Mozambique", "pl": "Mozambik", "tr": "Mozambik", "ro": "Mozambic"}),
    ("moldova",                     "373",  {"pt": "Moldávia", "en": "Moldova", "es": "Moldavia", "pl": "Mołdawia", "tr": "Moldova", "ro": "Republica Moldova"}),
    ("monaco",                      "377",  {"pt": "Mônaco", "en": "Monaco", "es": "Mónaco", "pl": "Monako", "tr": "Monako", "ro": "Monaco"}),
    ("mongolia",                    "976",  {"pt": "Mongólia", "en": "Mongolia", "es": "Mongolia", "pl": "Mongolia", "tr": "Moğolistan", "ro": "Mongolia"}),
    ("montenegro",                  "382",  {"pt": "Montenegro", "en": "Montenegro", "es": "Montenegro", "pl": "Czarnogóra", "tr": "Karadağ", "ro": "Muntenegru"}),
    ("montserrat",                  "1664", {"pt": "Montserrat", "en": "Montserrat", "es": "Montserrat", "pl": "Montserrat", "tr": "Montserrat", "ro": "Montserrat"}),

    # ── N ──────────────────────────────────────────────────────────────────────
    ("namibia",                     "264",  {"pt": "Namíbia", "en": "Namibia", "es": "Namibia", "pl": "Namibia", "tr": "Namibya", "ro": "Namibia"}),
    ("nauru",                       "674",  {"pt": "Nauru", "en": "Nauru", "es": "Nauru", "pl": "Nauru", "tr": "Nauru", "ro": "Nauru"}),
    ("nepal",                       "977",  {"pt": "Nepal", "en": "Nepal", "es": "Nepal", "pl": "Nepal", "tr": "Nepal", "ro": "Nepal"}),
    ("nicaragua",                   "505",  {"pt": "Nicarágua", "en": "Nicaragua", "es": "Nicaragua", "pl": "Nikaragua", "tr": "Nikaragua", "ro": "Nicaragua"}),
    ("niger",                       "227",  {"pt": "Níger", "en": "Niger", "es": "Níger", "pl": "Niger", "tr": "Nijer", "ro": "Niger"}),
    ("nigeria",                     "234",  {"pt": "Nigéria", "en": "Nigeria", "es": "Nigeria", "pl": "Nigeria", "tr": "Nijerya", "ro": "Nigeria"}),
    ("niue",                        "683",  {"pt": "Niue", "en": "Niue", "es": "Niue", "pl": "Niue", "tr": "Niue", "ro": "Niue"}),
    ("norway",                      "47",   {"pt": "Noruega", "en": "Norway", "es": "Noruega", "pl": "Norwegia", "tr": "Norveç", "ro": "Norvegia"}),
    ("new_caledonia",               "687",  {"pt": "Nova Caledônia", "en": "New Caledonia", "es": "Nueva Caledonia", "pl": "Nowa Kaledonia", "tr": "Yeni Kaledonya", "ro": "Noua Caledonie"}),
    ("new_zealand",                 "64",   {"pt": "Nova Zelândia", "en": "New Zealand", "es": "Nueva Zelanda", "pl": "Nowa Zelandia", "tr": "Yeni Zelanda", "ro": "Noua Zeelandă"}),

    # ── O ──────────────────────────────────────────────────────────────────────
    ("oman",                        "968",  {"pt": "Omã", "en": "Oman", "es": "Omán", "pl": "Oman", "tr": "Umman", "ro": "Oman"}),

    # ── P ──────────────────────────────────────────────────────────────────────
    ("netherlands",                 "31",   {"pt": "Países Baixos", "en": "Netherlands", "es": "Países Bajos", "pl": "Holandia", "tr": "Hollanda", "ro": "Țările de Jos"}),
    ("caribbean_netherlands",       "599",  {"pt": "Países Baixos Caribenhos", "en": "Caribbean Netherlands", "es": "Caribe neerlandés", "pl": "Niderlandy Karaibskie", "tr": "Karayip Hollandası", "ro": "Insulele Caraibe Olandeze"}),
    ("palau",                       "680",  {"pt": "Palau", "en": "Palau", "es": "Palaos", "pl": "Palau", "tr": "Palau", "ro": "Palau"}),
    ("panama",                      "507",  {"pt": "Panamá", "en": "Panama", "es": "Panamá", "pl": "Panama", "tr": "Panama", "ro": "Panama"}),
    ("papua_new_guinea",            "675",  {"pt": "Papua Nova Guiné", "en": "Papua New Guinea", "es": "Papúa Nueva Guinea", "pl": "Papua-Nowa Gwinea", "tr": "Papua Yeni Gine", "ro": "Papua-Noua Guinee"}),
    ("pakistan",                    "92",   {"pt": "Paquistão", "en": "Pakistan", "es": "Pakistán", "pl": "Pakistan", "tr": "Pakistan", "ro": "Pakistan"}),
    ("paraguay",                    "595",  {"pt": "Paraguai", "en": "Paraguay", "es": "Paraguay", "pl": "Paragwaj", "tr": "Paraguay", "ro": "Paraguay"}),
    ("peru",                        "51",   {"pt": "Peru", "en": "Peru", "es": "Perú", "pl": "Peru", "tr": "Peru", "ro": "Peru"}),
    ("french_polynesia",            "689",  {"pt": "Polinésia Francesa", "en": "French Polynesia", "es": "Polinesia Francesa", "pl": "Polinezja Francuska", "tr": "Fransız Polinezyası", "ro": "Polinezia Franceză"}),
    ("poland",                      "48",   {"pt": "Polônia", "en": "Poland", "es": "Polonia", "pl": "Polska", "tr": "Polonya", "ro": "Polonia"}),
    ("puerto_rico",                 "1787", {"pt": "Porto Rico", "en": "Puerto Rico", "es": "Puerto Rico", "pl": "Portoryko", "tr": "Porto Riko", "ro": "Puerto Rico"}),
    ("portugal",                    "351",  {"pt": "Portugal", "en": "Portugal", "es": "Portugal", "pl": "Portugalia", "tr": "Portekiz", "ro": "Portugalia"}),

    # ── Q ──────────────────────────────────────────────────────────────────────
    ("kenya",                       "254",  {"pt": "Quênia", "en": "Kenya", "es": "Kenia", "pl": "Kenia", "tr": "Kenya", "ro": "Kenya"}),
    ("kyrgyzstan",                  "996",  {"pt": "Quirguistão", "en": "Kyrgyzstan", "es": "Kirguistán", "pl": "Kirgistan", "tr": "Kırgızistan", "ro": "Kârgâzstan"}),
    ("kiribati",                    "686",  {"pt": "Quiribati", "en": "Kiribati", "es": "Kiribati", "pl": "Kiribati", "tr": "Kiribati", "ro": "Kiribati"}),

    # ── R ──────────────────────────────────────────────────────────────────────
    ("united_kingdom",              "44",   {"pt": "Reino Unido", "en": "United Kingdom", "es": "Reino Unido", "pl": "Wielka Brytania", "tr": "Birleşik Krallık", "ro": "Regatul Unit"}),
    ("central_african_republic",    "236",  {"pt": "República Centro-Africana", "en": "Central African Republic", "es": "República Centroafricana", "pl": "Republika Środkowoafrykańska", "tr": "Orta Afrika Cumhuriyeti", "ro": "Republica Centrafricană"}),
    ("democratic_republic_congo",   "243",  {"pt": "República Democrática do Congo", "en": "Democratic Republic of the Congo", "es": "República Democrática del Congo", "pl": "Demokratyczna Republika Konga", "tr": "Kongo - Kinşasa", "ro": "Congo - Kinshasa"}),
    ("dominican_republic",          "1809", {"pt": "República Dominicana", "en": "Dominican Republic", "es": "República Dominicana", "pl": "Dominikana", "tr": "Dominik Cumhuriyeti", "ro": "Republica Dominicană"}),
    ("czech_republic",              "420",  {"pt": "República Tcheca", "en": "Czech Republic", "es": "República Checa", "pl": "Czechy", "tr": "Çekya", "ro": "Cehia"}),
    ("reunion",                     "262",  {"pt": "Reunião", "en": "Réunion", "es": "Reunión", "pl": "Reunion", "tr": "Reunion", "ro": "Réunion"}),
    ("romania",                     "40",   {"pt": "Romênia", "en": "Romania", "es": "Rumanía", "pl": "Rumunia", "tr": "Romanya", "ro": "România"}),
    ("rwanda",                      "250",  {"pt": "Ruanda", "en": "Rwanda", "es": "Ruanda", "pl": "Rwanda", "tr": "Ruanda", "ro": "Rwanda"}),
    ("russia",                      "7",    {"pt": "Rússia", "en": "Russia", "es": "Rusia", "pl": "Rosja", "tr": "Rusya", "ro": "Rusia"}),

    # ── S ──────────────────────────────────────────────────────────────────────
    ("samoa",                       "685",  {"pt": "Samoa", "en": "Samoa", "es": "Samoa", "pl": "Samoa", "tr": "Samoa", "ro": "Samoa"}),
    ("american_samoa",              "1684", {"pt": "Samoa Americana", "en": "American Samoa", "es": "Samoa Americana", "pl": "Samoa Amerykańskie", "tr": "Amerikan Samoası", "ro": "Samoa Americană"}),
    ("san_marino",                  "378",  {"pt": "San Marino", "en": "San Marino", "es": "San Marino", "pl": "San Marino", "tr": "San Marino", "ro": "San Marino"}),
    ("saint_helena",                "290",  {"pt": "Santa Helena", "en": "St. Helena", "es": "Santa Elena", "pl": "Wyspa Świętej Heleny", "tr": "Saint Helena", "ro": "Sfânta Elena"}),
    ("saint_lucia",                 "1758", {"pt": "Santa Lúcia", "en": "Saint Lucia", "es": "Santa Lucía", "pl": "Saint Lucia", "tr": "Saint Lucia", "ro": "Sfânta Lucia"}),
    ("saint_barthelemy",            "590",  {"pt": "São Bartolomeu", "en": "St. Barthélemy", "es": "San Bartolomé", "pl": "Saint-Barthélemy", "tr": "Saint Barthelemy", "ro": "Saint-Barthélemy"}),
    ("saint_kitts_and_nevis",       "1869", {"pt": "São Cristóvão e Nevis", "en": "Saint Kitts and Nevis", "es": "San Cristóbal y Nieves", "pl": "Saint Kitts i Nevis", "tr": "Saint Kitts ve Nevis", "ro": "Saint Kitts și Nevis"}),
    ("saint_martin",                "590",  {"pt": "São Martinho", "en": "St. Martin", "es": "San Martín", "pl": "Saint-Martin", "tr": "Saint Martin", "ro": "Sfântul Martin"}),
    ("saint_pierre_and_miquelon",   "508",  {"pt": "São Pedro e Miquelão", "en": "St. Pierre & Miquelon", "es": "San Pedro y Miquelón", "pl": "Saint-Pierre i Miquelon", "tr": "Saint Pierre ve Miquelon", "ro": "Saint-Pierre și Miquelon"}),
    ("sao_tome_and_principe",       "239",  {"pt": "São Tomé e Príncipe", "en": "São Tomé and Príncipe", "es": "Santo Tomé y Príncipe", "pl": "Wyspy Świętego Tomasza i Książęca", "tr": "Sao Tome ve Principe", "ro": "São Tomé și Príncipe"}),
    ("saint_vincent_grenadines",    "1784", {"pt": "São Vicente e Granadinas", "en": "Saint Vincent and the Grenadines", "es": "San Vicente y las Granadinas", "pl": "Saint Vincent i Grenadyny", "tr": "Saint Vincent ve Grenadinler", "ro": "Saint Vincent și Grenadinele"}),
    ("senegal",                     "221",  {"pt": "Senegal", "en": "Senegal", "es": "Senegal", "pl": "Senegal", "tr": "Senegal", "ro": "Senegal"}),
    ("sierra_leone",                "232",  {"pt": "Serra Leoa", "en": "Sierra Leone", "es": "Sierra Leona", "pl": "Sierra Leone", "tr": "Sierra Leone", "ro": "Sierra Leone"}),
    ("serbia",                      "381",  {"pt": "Sérvia", "en": "Serbia", "es": "Serbia", "pl": "Serbia", "tr": "Sırbistan", "ro": "Serbia"}),
    ("seychelles",                  "248",  {"pt": "Seychelles", "en": "Seychelles", "es": "Seychelles", "pl": "Seszele", "tr": "Seyşeller", "ro": "Seychelles"}),
    ("singapore",                   "65",   {"pt": "Singapura", "en": "Singapore", "es": "Singapur", "pl": "Singapur", "tr": "Singapur", "ro": "Singapore"}),
    ("sint_maarten",                "1721", {"pt": "Sint Maarten", "en": "Sint Maarten", "es": "Sint Maarten", "pl": "Sint Maarten", "tr": "Sint Maarten", "ro": "Sint-Maarten"}),
    ("syria",                       "963",  {"pt": "Síria", "en": "Syria", "es": "Siria", "pl": "Syria", "tr": "Suriye", "ro": "Siria"}),
    ("somalia",                     "252",  {"pt": "Somália", "en": "Somalia", "es": "Somalia", "pl": "Somalia", "tr": "Somali", "ro": "Somalia"}),
    ("sri_lanka",                   "94",   {"pt": "Sri Lanka", "en": "Sri Lanka", "es": "Sri Lanka", "pl": "Sri Lanka", "tr": "Sri Lanka", "ro": "Sri Lanka"}),
    ("sudan",                       "249",  {"pt": "Sudão", "en": "Sudan", "es": "Sudán", "pl": "Sudan", "tr": "Sudan", "ro": "Sudan"}),
    ("south_sudan",                 "211",  {"pt": "Sudão do Sul", "en": "South Sudan", "es": "Sudán del Sur", "pl": "Sudan Południowy", "tr": "Güney Sudan", "ro": "Sudanul de Sud"}),
    ("sweden",                      "46",   {"pt": "Suécia", "en": "Sweden", "es": "Suecia", "pl": "Szwecja", "tr": "İsveç", "ro": "Suedia"}),
    ("switzerland",                 "41",   {"pt": "Suíça", "en": "Switzerland", "es": "Suiza", "pl": "Szwajcaria", "tr": "İsviçre", "ro": "Elveția"}),
    ("suriname",                    "597",  {"pt": "Suriname", "en": "Suriname", "es": "Surinam", "pl": "Surinam", "tr": "Surinam", "ro": "Suriname"}),

    # ── T ──────────────────────────────────────────────────────────────────────
    ("tajikistan",                  "992",  {"pt": "Tadjiquistão", "en": "Tajikistan", "es": "Tayikistán", "pl": "Tadżykistan", "tr": "Tacikistan", "ro": "Tadjikistan"}),
    ("thailand",                    "66",   {"pt": "Tailândia", "en": "Thailand", "es": "Tailandia", "pl": "Tajlandia", "tr": "Tayland", "ro": "Thailanda"}),
    ("taiwan",                      "886",  {"pt": "Taiwan", "en": "Taiwan", "es": "Taiwán", "pl": "Tajwan", "tr": "Tayvan", "ro": "Taiwan"}),
    ("tanzania",                    "255",  {"pt": "Tanzânia", "en": "Tanzania", "es": "Tanzania", "pl": "Tanzania", "tr": "Tanzanya", "ro": "Tanzania"}),
    ("british_indian_ocean_territory","246",  {"pt": "Território Britânico do Oceano Índico", "en": "British Indian Ocean Territory", "es": "Territorio Británico del Océano Índico", "pl": "Brytyjskie Terytorium Oceanu Indyjskiego", "tr": "Britanya Hint Okyanusu Toprakları", "ro": "Teritoriul Britanic din Oceanul Indian"}),
    ("palestine",                   "970",  {"pt": "Territórios palestinos", "en": "Palestinian Territories", "es": "Territorios Palestinos", "pl": "Terytoria Palestyńskie", "tr": "Filistin Bölgeleri", "ro": "Teritoriile Palestiniene"}),
    ("timor_leste",                 "670",  {"pt": "Timor-Leste", "en": "Timor-Leste", "es": "Timor Oriental", "pl": "Timor Wschodni", "tr": "Timor-Leste", "ro": "Timor-Leste"}),
    ("togo",                        "228",  {"pt": "Togo", "en": "Togo", "es": "Togo", "pl": "Togo", "tr": "Togo", "ro": "Togo"}),
    ("tokelau",                     "690",  {"pt": "Tokelau", "en": "Tokelau", "es": "Tokelau", "pl": "Tokelau", "tr": "Tokelau", "ro": "Tokelau"}),
    ("tonga",                       "676",  {"pt": "Tonga", "en": "Tonga", "es": "Tonga", "pl": "Tonga", "tr": "Tonga", "ro": "Tonga"}),
    ("trinidad_and_tobago",         "1868", {"pt": "Trinidad e Tobago", "en": "Trinidad and Tobago", "es": "Trinidad y Tobago", "pl": "Trynidad i Tobago", "tr": "Trinidad ve Tobago", "ro": "Trinidad și Tobago"}),
    ("tunisia",                     "216",  {"pt": "Tunísia", "en": "Tunisia", "es": "Túnez", "pl": "Tunezja", "tr": "Tunus", "ro": "Tunisia"}),
    ("turkmenistan",                "993",  {"pt": "Turcomenistão", "en": "Turkmenistan", "es": "Turkmenistán", "pl": "Turkmenistan", "tr": "Türkmenistan", "ro": "Turkmenistan"}),
    ("turkey",                      "90",   {"pt": "Turquia", "en": "Turkey", "es": "Turquía", "pl": "Turcja", "tr": "Türkiye", "ro": "Turcia"}),
    ("tuvalu",                      "688",  {"pt": "Tuvalu", "en": "Tuvalu", "es": "Tuvalu", "pl": "Tuvalu", "tr": "Tuvalu", "ro": "Tuvalu"}),

    # ── U ──────────────────────────────────────────────────────────────────────
    ("ukraine",                     "380",  {"pt": "Ucrânia", "en": "Ukraine", "es": "Ucrania", "pl": "Ukraina", "tr": "Ukrayna", "ro": "Ucraina"}),
    ("uganda",                      "256",  {"pt": "Uganda", "en": "Uganda", "es": "Uganda", "pl": "Uganda", "tr": "Uganda", "ro": "Uganda"}),
    ("uruguay",                     "598",  {"pt": "Uruguai", "en": "Uruguay", "es": "Uruguay", "pl": "Urugwaj", "tr": "Uruguay", "ro": "Uruguay"}),
    ("uzbekistan",                  "998",  {"pt": "Uzbequistão", "en": "Uzbekistan", "es": "Uzbekistán", "pl": "Uzbekistan", "tr": "Özbekistan", "ro": "Uzbekistan"}),

    # ── V ──────────────────────────────────────────────────────────────────────
    ("vanuatu",                     "678",  {"pt": "Vanuatu", "en": "Vanuatu", "es": "Vanuatu", "pl": "Vanuatu", "tr": "Vanuatu", "ro": "Vanuatu"}),
    ("venezuela",                   "58",   {"pt": "Venezuela", "en": "Venezuela", "es": "Venezuela", "pl": "Wenezuela", "tr": "Venezuela", "ro": "Venezuela"}),
    ("vietnam",                     "84",   {"pt": "Vietnã", "en": "Vietnam", "es": "Vietnam", "pl": "Wietnam", "tr": "Vietnam", "ro": "Vietnam"}),

    # ── W ──────────────────────────────────────────────────────────────────────
    ("wallis_and_futuna",           "681",  {"pt": "Wallis e Futuna", "en": "Wallis & Futuna", "es": "Wallis y Futuna", "pl": "Wallis i Futuna", "tr": "Wallis ve Futuna", "ro": "Wallis și Futuna"}),

    # ── Z ──────────────────────────────────────────────────────────────────────
    ("zambia",                      "260",  {"pt": "Zâmbia", "en": "Zambia", "es": "Zambia", "pl": "Zambia", "tr": "Zambiya", "ro": "Zambia"}),
    ("zimbabwe",                    "263",  {"pt": "Zimbábue", "en": "Zimbabwe", "es": "Zimbabue", "pl": "Zimbabwe", "tr": "Zimbabve", "ro": "Zimbabwe"}),
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
