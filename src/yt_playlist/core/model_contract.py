"""Named encoder and admission fields shared by runtime and preparation."""

FIELDS = {'rec_content_model':('recording','model'),
          'rec_content_generation':('recording','generation'),
          'rec_content_inputs':('recording','input_fingerprint'),
          'rec_content_genre_revision':('recording','genre_revision'),
          'rec_fit_inputs':('recording','fit_fingerprint'),
          'rec_artist_fit_inputs':('artist','fit_fingerprint'),
          'rec_artist_content_model':('artist','model')}
ARTIFACTS = tuple(key for key, (_owner,field) in FIELDS.items() if field == 'model')
FINGERPRINTS = tuple(key for key in FIELDS if key not in ARTIFACTS)
