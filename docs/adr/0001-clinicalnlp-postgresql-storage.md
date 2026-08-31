# Store ClinicalNLP searchable data in PostgreSQL

ER:ON will move versioned medical dictionaries, KCD data, policy documents, vectors, and approved-alias feedback from SQLite into the existing PostgreSQL/pgvector service so deployments share one managed source of truth. scispaCy models and the UMLS linker knowledge-base cache remain immutable runtime files because they are model assets rather than application records; SQLite remains available only as the migration source until PostgreSQL result parity is verified.
