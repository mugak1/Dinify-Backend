from typing import Optional
from users_app.models import User
from dinify_backend.configss.string_definitions import (
    TransactionType_Disbursement,
    TransactionType_OrderRefund,
    TransactionType_Subscription,
)
from users_app.controllers.otp_manager import OtpManager


def initiate_transaction(
    transaction_type: str,
    transaction_platform: str,
    actor: User,
    amount: Optional[int] = None,
    otp: Optional[str] = None,
    
):
    if transaction_type in [
        TransactionType_Disbursement,
        TransactionType_OrderRefund,
        TransactionType_Subscription
    ]:
        if otp is None:
            return {
                'status': 400,
                'message': 'Please provide the OTP.'
            }

        otp_verification = OtpManager().verify_otp(
            user_id=str(actor.id),
            otp=otp
        )
        if not otp_verification['data']['valid']:
            return {
                'status': 400,
                'message': 'Invalid OTP.'
            }

        return {
            'status': 200,
            'message': 'Transaction initiated successfully.'
        }
