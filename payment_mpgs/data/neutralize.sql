-- Clear MPGS secrets on neutralized (staging/duplicated) databases.
UPDATE payment_provider
   SET mpgs_api_password = NULL,
       mpgs_webhook_secret = NULL;
