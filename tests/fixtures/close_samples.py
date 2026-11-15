"""月结出账样例：首版运单与补发运单（历史修正 + 迟到运单 + 超范围行）。"""

from __future__ import annotations

HEADER = (
    "waybill_no,carrier,service,zone,pickup_date,weight,weight_unit,"
    "length,width,height,dim_unit,declared_value,pieces"
)

# 补发文件刻意包含三类行：
# - WB-1001 历史修正：同号改重量（3200g -> 6500g），计费重与应收都变，覆盖首次采信值
# - WB-1030 迟到运单：4 月新票，首版里没有
# - WB-1031 迟到运单：3 月票，落在本期之外，进超范围证据
RESEND = "\n".join([
    HEADER,
    "WB-1001,SF,standard,Z1,2026-04-03T02:15:00+08:00,6500,g,40,30,25,cm,2000.00,2",
    "WB-1030,YT,standard,Z2,2026-04-25T10:00:00+08:00,3,kg,30,20,20,cm,0,1",
    "WB-1031,SF,standard,Z1,2026-03-28T09:00:00+08:00,2,kg,20,20,20,cm,0,1",
]) + "\n"


def resend_bytes() -> bytes:
    return RESEND.encode("utf-8")


def rate_resend_bytes(base: bytes, set_version: int = 4) -> bytes:
    """在干净运价表基础上整体上调 4 月起 SF/Z1 的首重与续重价，并抬高运价集版本。"""
    lines = base.decode("utf-8").splitlines()
    out = [lines[0]]
    for line in lines[1:]:
        cells = line.split(",")
        if cells[0] == "SF" and cells[2] == "Z1" and cells[3].endswith("-v2"):
            cells[7] = "14.00"  # first_price
            cells[9] = "2.80"   # step_price
            cells[10] = "21.00"  # min_charge
        cells[-1] = str(set_version)
        out.append(",".join(cells))
    return ("\n".join(out) + "\n").encode("utf-8")
