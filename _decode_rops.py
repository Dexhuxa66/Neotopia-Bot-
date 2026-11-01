import struct as st

def parse_op8(hexstr):
    d = bytes.fromhex(hexstr)
    # 08 | u16 seq @1 | u16 item @3 | u32 x @5 | u32 y @9 | u16 count @13 | str drop_id | str source
    seq   = int.from_bytes(d[1:3], 'big')
    item  = int.from_bytes(d[3:5], 'little')
    x_raw = int.from_bytes(d[5:9], 'little')
    y_raw = int.from_bytes(d[9:13], 'little')
    count = int.from_bytes(d[13:15], 'big')
    idx = 15
    dl = d[idx]; idx += 1
    drop_id = d[idx:idx+dl].decode(); idx += dl
    sl = d[idx]; idx += 1
    source = d[idx:idx+sl].decode()
    return dict(seq=seq, item=item, x_raw=x_raw, y_raw=y_raw, count=count, drop_id=drop_id, source=source)

# sample1: DIRT x23
s1 = parse_op8("080000030017452c0000442ac0001764726f705f313739323737353237323339325f393839381c574643646456763644585a62586962675573726f394a77444a4b6a32")
print("sample1:", s1)
# DIRT x29 at (2528, 779)
# x_raw=17687, y_raw=17408
# 17687 / 2528 = 7.0?  17687/7 = 2526.7 ~ 2528!
# 17408 / 779 = 22.3?  no.  17408/779 = 22.34
# Hmm. 2528*7=17696 close to 17687. 779*22.3=17408.
# Maybe x = px * 7.00? or tile*224? 79*224=17696. 
# 79 tile * 224 = 17696. y 24 tile * 725 = 17400.
# Let me try: x_px = x_raw / 7 -> 17687/7 = 2526.7. Not exact.
# Or these are 12-bit or fixed point. 17687 = 0x4517. 0x45=69, 0x17=23.
# 0x0045 = 69? 0x17 = 23. 
# Actually: x u16 LE @5:7 = 0x4517 = 17687, y u16 LE @9:11 = 0x4400 = 17408
# 17687 in binary: 0b100010100010111. 
# Maybe x = 0x4517 >> some_shift? 0x4517 = 17687. 17687 >> 3 = 2210. >> 4 = 1105.
# 2528 / 17687 = 0.1429 = 1/7. So x_px = x_raw / 7.0!
# 17408 / 779 = 22.35. Not 7.
# But wait — maybe x and y are in DIFFERENT units. x in "horizontal tile*28"? 
# 79 * 224 = 17696. 17687/224 = 78.96 ~ 79! 
# 24 * 725 = 17400. 17408/725 = 24.01 ~ 24!
# So x = tile_x * 224, y = tile_y * 725? 224=7*32, 725=22.65*32?
# 224/32=7, 725/32=22.656. Asymmetric.
# More likely: x_px = x_raw * 32 / 224 = x_raw/7. y_px = y_raw * 32/725.
# Let me just compute with 2528,779 target:
print("x_px est:", s1['x_raw']/7, "y_px est:", s1['y_raw']/(17408/779))
