-- pc1.proceduresestimate: index the procedure_desc column.
--
-- pc1.orders_v joins orders.procedure_description = proceduresestimate.procedure_desc
-- (the cpt_codes table is eliminated; orders link to the catalog by description
-- text). Per the "view perf == direct joins" guarantee (session-handoff.md
-- 8.1), the join column on the larger/looked-up side must be indexed so the
-- planner can probe the catalog by description rather than seq-scan it.
--
-- Idempotent: CREATE INDEX IF NOT EXISTS.

CREATE INDEX IF NOT EXISTS idx_pe_procedure_desc
    ON pc1.proceduresestimate USING btree (procedure_desc) TABLESPACE pg_default;
