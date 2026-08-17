"""Result-metadata type codes.

`cursor.description[i].type_code` is one of these numbers rather than a type name, so
callers can branch on a family (all the fixed-point spellings share one code) without
parsing SQL type text. `type_code_for` maps an engine type name onto its code.
"""

FIELD_ID_TO_NAME = {
    0: "FIXED",
    1: "REAL",
    2: "TEXT",
    3: "DATE",
    4: "TIMESTAMP",
    5: "VARIANT",
    6: "TIMESTAMP_LTZ",
    7: "TIMESTAMP_TZ",
    8: "TIMESTAMP_NTZ",
    9: "OBJECT",
    10: "ARRAY",
    11: "BINARY",
    12: "TIME",
    13: "BOOLEAN",
}

FIELD_NAME_TO_ID = {name: field_id for field_id, name in FIELD_ID_TO_NAME.items()}


def type_code_for(data_type):
    """Map a Frostlake wire dataType onto its numeric type code."""
    t = (data_type or "").upper()
    base = t.split("(", 1)[0].strip()
    if base in ("NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT",
                "SMALLINT", "TINYINT", "BYTEINT", "FIXED"):
        return FIELD_NAME_TO_ID["FIXED"]
    if base in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "DOUBLE PRECISION", "REAL"):
        return FIELD_NAME_TO_ID["REAL"]
    if base == "DATE":
        return FIELD_NAME_TO_ID["DATE"]
    if base == "TIME":
        return FIELD_NAME_TO_ID["TIME"]
    if base in ("TIMESTAMP", "DATETIME", "TIMESTAMP_NTZ"):
        return FIELD_NAME_TO_ID["TIMESTAMP_NTZ"]
    if base == "TIMESTAMP_LTZ":
        return FIELD_NAME_TO_ID["TIMESTAMP_LTZ"]
    if base == "TIMESTAMP_TZ":
        return FIELD_NAME_TO_ID["TIMESTAMP_TZ"]
    if base == "VARIANT":
        return FIELD_NAME_TO_ID["VARIANT"]
    if base == "OBJECT":
        return FIELD_NAME_TO_ID["OBJECT"]
    if base == "ARRAY":
        return FIELD_NAME_TO_ID["ARRAY"]
    if base in ("BINARY", "VARBINARY"):
        return FIELD_NAME_TO_ID["BINARY"]
    if base == "BOOLEAN":
        return FIELD_NAME_TO_ID["BOOLEAN"]
    return FIELD_NAME_TO_ID["TEXT"]
