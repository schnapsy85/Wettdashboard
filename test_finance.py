import server

def main():
    rows = server.finance_parse_csv('Datum;Beschreibung;Betrag\n31.12.2025;Supermarkt;-12,50\n2025-12-31;Kartenabrechnung;-12.50\n'.encode('cp1252'), 'checking')
    assert rows[0]['amount_cents'] == -1250
    assert rows[0]['kind'] == 'expense' and rows[0]['review_status'] == 'suggested'
    assert rows[1]['kind'] == 'transfer' and rows[1]['review_status'] == 'unclear'
    assert server.finance_date('01.02.2026') == '2026-02-01'
    assert server.finance_amount('1.234,56') == 123456
    print('finance self-check: OK')

if __name__ == '__main__':
    main()
