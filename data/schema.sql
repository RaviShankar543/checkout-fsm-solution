CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            birth_date DATE,
            gender TEXT CHECK(gender IN ('Male','Female')),
            address TEXT,
            latitude REAL,
            longitude REAL,
            per_capita_income_usd_cents INTEGER,
            yearly_income_usd_cents INTEGER,
            total_debt_usd_cents INTEGER,
            credit_score INTEGER
        );
CREATE TABLE mcc_codes (
            mcc INTEGER PRIMARY KEY,
            description TEXT
        );
CREATE TABLE merchants (
            id INTEGER PRIMARY KEY,
            name TEXT,
            mcc INTEGER,
            FOREIGN KEY (mcc) REFERENCES mcc_codes(mcc)
        );
CREATE TABLE merchant_locations (
            id INTEGER PRIMARY KEY,
            merchant_id INTEGER,
            city TEXT,
            state TEXT,
            zip INTEGER,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id)
        );
CREATE TABLE cards (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            card_brand TEXT CHECK(card_brand IN ('Amex','Discover','Mastercard','Visa')),
            card_type TEXT CHECK(card_type IN ('Credit','Debit','Debit (Prepaid)')),
            expires DATE,
            has_chip BOOLEAN,
            credit_limit_usd_cents INTEGER,
            acct_open_date DATE,
            year_pin_last_changed INTEGER,
            card_on_dark_web BOOLEAN,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
CREATE TABLE fraud_labels (
            transaction_id TEXT PRIMARY KEY,
            is_fraud BOOLEAN
        );
CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            date DATETIME,
            card_id INTEGER,
            amount_usd_cents INTEGER,
            transaction_type TEXT CHECK(transaction_type IN ('Chip Transaction','Online Transaction','Swipe Transaction')),
            merchant_id INTEGER,
            merchant_location_id INTEGER,
            errors TEXT CHECK(errors IN ('Bad CVV','Bad Card Number','Bad Expiration','Bad PIN','Bad Zipcode','Insufficient Balance','Technical Glitch')),
            FOREIGN KEY (card_id) REFERENCES cards(id),
            FOREIGN KEY (merchant_id) REFERENCES merchants(id),
            FOREIGN KEY (merchant_location_id) REFERENCES merchant_locations(id)
        );
