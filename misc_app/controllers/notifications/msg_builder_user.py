def make_user_messages(msg_data, footer) -> dict:
    message = {
        'email': None,
        'sms': None,
        'subject': None
    }

    if msg_data.get('msg_type') == 'new-user':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data.get('first_name', '')},</span></p>
        <p>&nbsp;</p>
        <p><span style="font-weight: 400;">We are excited to have you join us.&nbsp;</span></p>
        <p><span style="font-weight:400;">Thank you for choosing Dinify. &nbsp;</span></p>
        {footer}
        """
        message = {
            'subject': 'Welcome to Dinify!',
            'email': email,
            'sms': None
        }

    elif msg_data.get('msg_type') == 'new-user-credentials':
        email = f"""
        <p><span style="font-weight: 400;">Hello {msg_data.get('first_name', '')},</span></p>
        <p>&nbsp;</p>
        <p><span style="font-weight: 400;">Your Dinify credentials are <br>Username: {msg_data.get('username')}<br>Password: {msg_data.get('password')}.&nbsp;</span></p>
        <p><span style="font-weight:400;">Thank you for choosing Dinify. &nbsp;</span></p>
        {footer}
        """
        sms = f"Dinify login\nUsername: {msg_data.get('username')}\nPassword: {msg_data.get('password')}."  # noqa
        message = {
            'subject': 'Dinify Credentials!',
            'email': email,
            'sms': sms
        }

    elif msg_data.get('msg_type') == 'user-update':
        email = f"""
        <p><span style="font-weight:400;">Hello {msg_data.get('first_name', '')},</span></p>
        <p><span style="font-weight:400;">Your Dinify account has been updated.&nbsp;</span></p>
        <p>&nbsp;</p>
        <p><span style="font-weight:400;">If you are aware of this change, you are all set.&nbsp;</span></p>
        <p>&nbsp;</p>
        <p><span style="font-weight:400;">Didn’t make this change? Please take these steps to secure your account.&nbsp;</span></p>
        <p>&nbsp;</p>
        {footer}
        """
        message = {
            'subject': 'Dinify Account Details Updated',
            'email': email,
            'sms': "Your Dinify details have been updated."
        }

    elif msg_data.get('msg_type') == 'password-change':
        email = f"""
        <p><span style="font-weight:400;">Hello {msg_data.get('first_name', '')},</span></p>
        <p><span style="font-weight:400;">Your Dinify password has been changed.&nbsp;</span></p>
        <p><span style="font-weight:400;">If you made this change, you are all set.&nbsp;</span></p>
        <p>&nbsp;</p>
        <p><span style="font-weight:400;">Didn’t reset your password? Please take these steps to secure your account.&nbsp;</span></p>
        <p>&nbsp;</p>
        {footer}
        """
        message = {
            'subject': 'Dinify Password Changed',
            'email': email,
            'sms': 'Your Dinify password has been changed.'
        }

    elif msg_data.get('msg_type') == 'forgot-password':
        email = f"""
        <p><span style="font-weight:400;">Hello {msg_data.get('first_name', '')},</span></p>
        <p><span style="font-weight:400;">Your One-Time Dinify password is {msg_data['password']}.&nbsp;</span></p>
        <p>&nbsp;</p>
        <p><span style="font-weight:400;">Didn’t reset your password? Please take these steps to secure your account.&nbsp;</span></p>
        {footer}
        """
        message = {
            'subject': 'Dinify Password Reset',
            'email': email,
            'sms': f"Your Dinify one-time password is {msg_data['password']}"
        }

    elif msg_data.get('msg_type') == 'otp':
        email = f"""
        <p><span style="font-weight:400;">Hello {msg_data.get('first_name', '')},</span></p>
        <p><span style="font-weight:400;">Your Dinify OTP is {msg_data['otp']}.&nbsp;</span></p>
        <p>&nbsp;</p>
        {footer}
        """
        sms = f"Your Dinify OTP is {msg_data['otp']}."
        message = {
            'subject': 'Dinify OTP',
            'email': email,
            'sms': sms
        }

    return message
