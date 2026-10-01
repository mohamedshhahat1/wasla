"""What every Meta product's webhook shares, whichever channel it carries.

WhatsApp, Instagram and Messenger deliveries are signed the same way - with the
app secret, over the exact bytes sent - and differ in everything after that. The
signature lives here so a second channel's route verifies with this code rather
than a copy of it (OMNI-010); what each payload means stays with its adapter.
"""
