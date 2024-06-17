def internationalise_msisdn(
    country: str,
    msisdn: str
) -> str:
    if country in ['UG', 'ug', 'Uganda']:
        if msisdn.startswith('0'):
            return f'256{msisdn[1:]}'
        if msisdn.startswith('7'):
            return f'256{msisdn}'
        if msisdn.startswith('256'):
            return f'256{msisdn[3:]}'
        if msisdn.startswith('256'):
            return msisdn
