CREATE TABLE anomalies (
    anomaly_id SERIAL PRIMARY KEY,
    invoice_document_id INT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    anomaly_type VARCHAR(50) NOT NULL,          -- 'amount' | 'round' | 'weekend' | 'duplicate'
    severity VARCHAR(20) NOT NULL,              -- 'high' | 'medium' | 'low'
    detected_pattern JSONB,                     -- raw signals: e.g. {"current":25000, "avg":1850, "deviation_pct":1251}
    ai_explanation TEXT,                        -- Gemini's natural language explanation
    ai_recommendation TEXT,                     -- Gemini's suggested action
    status VARCHAR(20) DEFAULT 'pending',       -- 'pending' | 'reviewed' | 'dismissed'
    reviewed_by INT REFERENCES users(user_id),
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_anomalies_status ON anomalies(status);
CREATE INDEX idx_anomalies_invoice ON anomalies(invoice_document_id);
