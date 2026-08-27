-- =====================================================================
-- ER:ON — 참조 무결성 제약
--
-- 대량 COPY 적재가 끝난 뒤에 건다. 적재 순서에 의존하지 않게 하기 위함이며,
-- 제약이 걸리는 시점에 고아 행이 있으면 적재가 실패한다(= 조용히 넘어가지 않음).
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_edstays_subject') THEN
        ALTER TABLE mimic.edstays
            ADD CONSTRAINT fk_edstays_subject
            FOREIGN KEY (subject_id) REFERENCES mimic.patients(subject_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_edstays_hadm') THEN
        ALTER TABLE mimic.edstays
            ADD CONSTRAINT fk_edstays_hadm
            FOREIGN KEY (hadm_id) REFERENCES mimic.admissions(hadm_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_triage_stay') THEN
        ALTER TABLE mimic.triage
            ADD CONSTRAINT fk_triage_stay
            FOREIGN KEY (stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_vitalsign_stay') THEN
        ALTER TABLE mimic.ed_vitalsign
            ADD CONSTRAINT fk_vitalsign_stay
            FOREIGN KEY (stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_diagnosis_stay') THEN
        ALTER TABLE mimic.ed_diagnosis
            ADD CONSTRAINT fk_diagnosis_stay
            FOREIGN KEY (stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_icustays_hadm') THEN
        ALTER TABLE mimic.icustays
            ADD CONSTRAINT fk_icustays_hadm
            FOREIGN KEY (hadm_id) REFERENCES mimic.admissions(hadm_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_prediction_stay') THEN
        ALTER TABLE app.prediction
            ADD CONSTRAINT fk_prediction_stay
            FOREIGN KEY (ed_stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_alias_stay') THEN
        ALTER TABLE app.patient_alias
            ADD CONSTRAINT fk_alias_stay
            FOREIGN KEY (ed_stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_demo_stay') THEN
        ALTER TABLE app.demo_stay
            ADD CONSTRAINT fk_demo_stay
            FOREIGN KEY (ed_stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_bed_assign_stay') THEN
        ALTER TABLE app.bed_assignment
            ADD CONSTRAINT fk_bed_assign_stay
            FOREIGN KEY (ed_stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_alert_stay') THEN
        ALTER TABLE app.alert
            ADD CONSTRAINT fk_alert_stay
            FOREIGN KEY (ed_stay_id) REFERENCES mimic.edstays(stay_id);
    END IF;
END $$;
