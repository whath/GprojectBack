from marketdata.operations import alert

alert('tls_renewal_validation_failed',
      'Renewal dry-run failed: public HTTP ACME request was intercepted by Alibaba Cloud with 403 Non-compliance ICP Filing. HTTPS initially passed. Verify ICP domain and Alibaba Cloud access registration before declaring public readiness.',
      'error')
