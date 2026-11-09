{
    'name': "Payment Provider: Mastercard Gateway (MPGS) Hosted Checkout",
    'version': '17.0.1.0.1',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "Card payments and website donations through Mastercard Payment Gateway "
               "Services (MPGS) Hosted Checkout, e.g. Bank of Beirut.",
    'description': " ",  # Non-empty string to avoid loading the README file.
    'author': "ISTIDAMA",
    'depends': ['payment', 'website_payment'],
    'data': [
        'views/payment_mpgs_templates.xml',
        'views/payment_provider_views.xml',
        'data/payment_provider_data.xml',
        'data/ir_cron_data.xml',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'license': 'LGPL-3',
}
