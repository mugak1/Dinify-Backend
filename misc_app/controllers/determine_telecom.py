from typing import Optional
from dinify_backend.configs import AirtelUg, MtnUg


def determine_telecom(msisdn: str) -> Optional[str]:
    """
    - Determines the telecom to which the msisdn belongs
    """
    # get the first 5 digits of the msisdn
    telecom_strings = msisdn[:5]

    if telecom_strings in ["25670", "25675", "25674"]:
        return AirtelUg
    elif telecom_strings in ["25676", "25677", "25678"]:
        return MtnUg
    return None
