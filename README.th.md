# glab-teleport

**ย้าย group และ project ของ GitLab ไปยัง GitLab อีกเครื่องหนึ่ง พร้อมตรวจสอบหลังย้ายว่า branch, tag, CI/CD variables และ environment scope ตรงกันครบทุกรายการ**

[English](README.md) · [ติดตั้ง](#ติดตั้ง) · [เริ่มต้นใช้งาน](#เริ่มต้นใช้งาน) · [คำสั่งทั้งหมด](#คำสั่งทั้งหมด) · [Sync](#อัปเดตปลายทางให้ตามต้นทาง-sync) · [หลักการทำงาน](#หลักการทำงาน) · [ความปลอดภัย](#ความปลอดภัยและความเป็นส่วนตัว) · [คำถามที่พบบ่อย](#คำถามที่พบบ่อย)

---

การย้ายจาก GitLab เครื่องหนึ่งไปอีกเครื่องหนึ่งมักต้องเขียนสคริปต์ `git push --mirror` เอง คัดลอก CI/CD variables ทีละตัว และยังไม่แน่ใจว่าตกหล่นอะไรไปหรือไม่ `glab-teleport` ทำงานทั้งหมดนี้ได้ในคำสั่งเดียว แสดงแผนให้ตรวจก่อนเริ่ม และสรุปผลด้วยรายงานที่ยืนยันว่าต้นทางและปลายทางตรงกัน

```text
$ glab-teleport --lang th group payments platform/payments

แผนการย้าย  group · payments → platform/payments
  โครงสร้าง   keep — คงโครงสร้าง subgroup เดิม
  สิ่งที่ย้าย   repo  env  runner  settings  extras
  path        นับจาก payments/ → platform/payments/

  สถานะ          ต้นทาง               ปลายทาง
  ✓ ตรงกันแล้ว   api                  api                  code ตรงกันแล้ว
  ↻ อัปเดต       web                  web                  ขาด 2 refs
  + สร้างใหม่    services/ledger      services/ledger
  + สร้างใหม่    services/invoicing   services/invoicing

  4 project · 2 สร้างใหม่ · 1 ตรงกันแล้ว · 1 อัปเดต

เริ่มย้าย 4 project หรือไม่ [y/N] y

กำลังย้าย  4 project · ทำพร้อมกัน 4
  ✓ payments/api                  repo ✓  env ✓  runner ✓  settings ✓  extras ✓   6s
  ✓ payments/web                  repo ✓  env ✓  runner ✓  settings ✓  extras ✓   9s
  ✓ payments/services/ledger      repo ✓  env ✓  runner –  settings ✓  extras ✓  14s
  ☞ payments/services/invoicing   repo ✓  env ☞  runner –  settings ✓  extras ✓  12s

ผลการย้าย  group · payments → platform/payments · 41s

                     ต้นทาง  ปลายทาง
  Project                4        4  ✓
  Branch                23       23  ✓
  Tag                   11       11  ✓
  Protected rules        9        9  ✓
  CI/CD variables       64       64  ✓
    scope *             28       28
    scope production    18       18
    scope staging       18       18
  Environments           6        6  ✓

  ✓ 3 ตรงกันครบ   ☞ 1 ต้องดำเนินการเอง

  รายการที่ต้องตรวจสอบ
  ☞ payments/services/invoicing
      variables: 1 × ต้นทางซ่อนค่าไว้ — ตรวจค่าไม่ได้: STRIPE_KEY [production]

  รายงาน  ~/.glab-teleport/runs/20261006-104512-group-payments/report.md
```

## ความสามารถ

- **ย้ายได้ทั้ง group หรือทีละ project:**
  - group ที่มี subgroup ซ้อนกันกี่ชั้นก็ได้
  - เลือกหลาย project พร้อมกันแล้วย้ายไปไว้ใน group เดียวกันได้
- **ย้ายครบทุกอย่างที่ project ต้องใช้:**
  - ทุก branch และ tag รวม Git LFS และ wiki
  - CI/CD variables ทุก environment scope ทั้งระดับ group และ project
  - environments, protected branch/tag และค่าตั้งของ project
  - runner, pipeline schedules, webhooks, deploy keys และ members
- **เลือกได้ว่าจะย้ายอะไร:** `--only repo`, `--only env`, `--only runner,env` หรือย้ายทั้งหมด (ค่าเริ่มต้น)
- **จับคู่ project ด้วย commit จริง:**
  - project ที่มีอยู่ปลายทางแล้วจะถูกพบจาก commit แม้จะเปลี่ยนชื่อหรือย้ายตำแหน่งไปแล้ว
  - ระบบอัปเดต project นั้นแทนการสร้างซ้ำ
  - จะไม่เขียนทับ code ที่ไม่เกี่ยวข้องกัน
- **กำหนดโครงสร้างปลายทางได้เอง:**
  - คงโครงสร้างเดิม ตัด subgroup ออก หรือรวมชื่อ subgroup เข้ากับชื่อ project
  - `keep` (ค่าเริ่มต้น) เหมือนต้นทางทุกชั้น ส่วน `auto` จะทำตามแบบที่ปลายทางใช้อยู่แล้ว
  - `--relocate` ย้าย project ที่เคยย้ายไว้แบบไม่สอดคล้องกันให้เข้าที่
- **ตรวจสอบได้จริง ไม่ใช่แค่เชื่อว่าสำเร็จ:**
  - หลังย้ายทุกครั้ง ระบบเทียบตัวแปรทีละคู่ `(key, environment_scope)` ทั้งค่า type, protected, masked และ raw
  - เทียบ SHA ของทุก branch และ tag
  - ออกรายงาน `report.md` และ `report.json`
- **หน้าจอสะอาด ใช้ได้ทั้งภาษาไทยและอังกฤษ:**
  - โหมดเลือกจากรายการพร้อมช่องค้นหา (พิมพ์ `glab-teleport`)
  - ระหว่างย้ายแสดงหนึ่งบรรทัดต่อ project ส่วนรายละเอียดอยู่ในรายงาน
- **ใช้ได้กับทุก GitLab:**
  - self-managed และ gitlab.com
  - เข้าสู่ระบบด้วย token หรือ OAuth
  - ไม่ต้องติดตั้งไลบรารีเพิ่ม ใช้แค่ Python 3.9 ขึ้นไปและ git

## ติดตั้ง

```bash
npm install -g glab-teleport
```

หรือติดตั้งผ่าน pipx โดยไม่ต้องใช้ Node:

```bash
pipx install git+https://github.com/Aekawan/glab-teleport
```

สิ่งที่ต้องมีในเครื่อง:

- **Python 3.9 ขึ้นไป:** Python ที่มากับ macOS ใช้ได้
- **git**
- **git-lfs:** จำเป็นเฉพาะ repository ที่ใช้ Git LFS

## เริ่มต้นใช้งาน

### 1. ตั้งภาษาไทยเป็นค่าเริ่มต้น (ถ้าต้องการ)

```bash
glab-teleport config lang th
```

ถ้าต้องการใช้ภาษาไทยเฉพาะครั้ง ให้ใส่ `--lang th` ต่อท้ายคำสั่งใดก็ได้ หรือตั้ง environment variable `GLAB_TELEPORT_LANG=th`

### 2. เข้าสู่ระบบทั้งสองฝั่ง

```bash
glab-teleport login
```

โปรแกรมจะถาม URL ของ GitLab **ต้นทาง** และ **ปลายทาง** แล้วให้เข้าสู่ระบบทีละฝั่ง ต้องเข้าสู่ระบบทั้งสองฝั่ง เพราะโปรแกรมดึงข้อมูลจากต้นทางด้วยสิทธิ์ของต้นทาง แล้วส่งเข้าปลายทางด้วยสิทธิ์ของปลายทาง ข้อมูลวิ่งผ่านเครื่องของคุณ เซิร์ฟเวอร์ทั้งสองเครื่องจึงไม่จำเป็นต้องเชื่อมต่อถึงกัน

วิธีเข้าสู่ระบบที่เลือกได้:

| วิธี | ใช้เมื่อ |
|---|---|
| **สร้าง token ผ่านเว็บ** (แนะนำ) | ใช้ได้กับ GitLab ทุกเครื่อง โปรแกรมจะเปิดหน้าสร้าง token ที่กรอกชื่อและ scope ไว้ให้แล้ว เพียงตั้งวันหมดอายุ กด *Create* แล้วนำ token มาวาง |
| **OAuth ผ่าน browser** | เข้าสู่ระบบผ่านหน้าเว็บแบบเดียวกับ `glab auth login --web` ต้องมี OAuth application บน GitLab นั้นก่อน (ดูวิธีสร้างด้านล่าง) token จะต่ออายุเองอัตโนมัติ |
| **วาง token ที่มีอยู่แล้ว** | ใช้ personal access token, group access token หรือ project access token ที่มีอยู่ |

scope และสิทธิ์ที่ต้องมี:

| | scope | สิทธิ์ (role) |
|---|---|---|
| ต้นทาง | `read_api`, `read_repository` | Maintainer ของ project ต้นทาง (จำเป็นสำหรับการอ่าน CI/CD variables) |
| ปลายทาง | `api`, `write_repository` | Maintainer ของ group ปลายทาง (ถ้าจะสร้าง group runner หรือย้ายตำแหน่ง project ด้วย `--relocate` ต้องเป็น Owner) |

**สร้าง OAuth application** (ทำครั้งเดียวต่อ GitLab หนึ่งเครื่อง ผู้ใช้ทั่วไปสร้างเองได้):

1. ไปที่ *User Settings → Applications* ของ GitLab นั้น
2. ตั้งชื่อ `glab-teleport` และตั้ง Redirect URI เป็น `http://localhost:7171/auth/redirect`
3. ไม่ต้องเลือก *Confidential* แล้วเลือก scope `api`, `read_api`, `read_repository` และ `write_repository`
4. บันทึกแล้วนำ *Application ID* มาวางตอนที่โปรแกรมถาม

ถ้าเคยตั้ง OAuth ให้ `glab` ไว้กับ GitLab นั้นแล้ว โปรแกรมจะใช้ Application ID เดิมให้อัตโนมัติ

### 3. ตรวจความพร้อม

```bash
glab-teleport doctor
```

คำสั่งนี้ตรวจรายการต่อไปนี้ และบอกวิธีแก้หากพบปัญหา:

- การเชื่อมต่อเครือข่าย (DNS/VPN)
- ข้อมูลเข้าสู่ระบบ
- วันหมดอายุของ token
- scope และสิทธิ์ใน group ปลายทาง
- การเข้าถึง git ได้จริง

### 4. เริ่มย้าย

```bash
glab-teleport
```

โหมดเลือกจากรายการจะพาไปทีละขั้น:

1. เลือกว่าจะย้ายทั้ง group หรือเลือกเป็นราย project
2. เลือกต้นทางจากรายการ พิมพ์เพื่อค้นหาได้ (ถ้าพิมพ์หลายคำ รายการต้องมีครบทุกคำ)
3. เลือกปลายทาง ตัวที่ระบบแนะนำจะมีเครื่องหมาย ★ อยู่บนสุด หรือพิมพ์ path ใหม่เพื่อให้สร้าง subgroup ก็ได้
4. เลือกสิ่งที่จะย้าย กด Tab เพื่อเลือกหรือยกเลิก
5. เลือกโครงสร้าง subgroup และตัวเลือกเพิ่มเติม
6. ตรวจแผนแล้วยืนยัน ระบบจะย้าย ตรวจสอบ และสรุปผลให้

ปุ่มที่ใช้ในรายการ:

- **↑ ↓:** เลื่อน
- **Enter:** เลือก
- **Tab:** เลือกหลายรายการ
- **Ctrl-A:** เลือกทั้งหมดที่กรองอยู่
- **Esc:** ล้างคำค้น หรือย้อนกลับ
- **Ctrl-C:** ออก

ท้ายแผนจะแสดงคำสั่งแบบพิมพ์ตรงให้ด้วย เพื่อนำไปใช้ซ้ำหรือใส่ในสคริปต์

## คำสั่งทั้งหมด

```bash
glab-teleport                                         # โหมดเลือกจากรายการ
glab-teleport group  <group ต้นทาง> <group ปลายทาง>    # ย้ายทั้ง group รวม subgroup
glab-teleport project <project ต้นทาง>... <ปลายทาง>    # ย้าย project ไปไว้ใน group หรือกำหนด path เอง
glab-teleport sync [ต้นทาง]                           # อัปเดตปลายทางให้ตามต้นทางล่าสุด
glab-teleport verify <ต้นทาง> <ปลายทาง>               # ตรวจเทียบโดยไม่เปลี่ยนแปลงอะไร
glab-teleport audit [group ต้นทาง] [group ปลายทาง]     # สถานะการย้ายทั้งระบบ
glab-teleport refs [ปลายทาง]                          # ค้นหาจุดที่ยังอ้างถึง GitLab ต้นทาง
glab-teleport repoint                                 # สร้างสคริปต์เปลี่ยน git remote ในเครื่องของทีม
glab-teleport report [latest|<รหัสรอบ>]               # ดูประวัติหรือสรุปผลของแต่ละรอบ
glab-teleport login | logout | doctor | config
```

ตัวอย่างการใช้งาน:

```bash
# ย้ายทั้ง group
glab-teleport group payments platform/payments

# ดูแผนอย่างเดียว ยังไม่ย้ายจริง
glab-teleport group payments platform/payments --dry-run

# ย้ายเฉพาะ code กับ CI/CD variables
glab-teleport group payments platform/payments --only repo,env

# ย้ายแค่ CI/CD variables ของ project ที่ย้าย code ไปแล้ว
glab-teleport project payments/api platform/payments/api --only env

# ย้ายหลาย project ไปไว้ใน group เดียวกัน (ใช้ชื่อเดิม)
glab-teleport project payments/api payments/web platform/payments

# คงโครงสร้าง subgroup เหมือนต้นทางทุกชั้น และย้าย project ที่เคยย้ายไว้ผิดที่ให้เข้าที่
glab-teleport group payments platform/payments --layout keep --relocate

# ย้ายเฉพาะบาง subgroup
glab-teleport group payments platform/payments --include services --exclude services/legacy

# ใช้ใน CI หรือสคริปต์ (ไม่ถามยืนยัน)
glab-teleport group payments platform/payments -y
```

ทางลัด: `glab-teleport payments platform/payments` โปรแกรมจะดูให้เองว่าต้นทางเป็น group หรือ project และใส่ path, URL หน้าเว็บ หรือ git URL ก็ได้

### เลือกสิ่งที่จะย้าย (`--only`)

| ค่า | สิ่งที่ย้าย |
|---|---|
| `repo` | branch และ tag ทั้งหมด (รวม LFS), wiki, default branch, protected branch/tag |
| `env` | CI/CD variables ทุก environment scope (ระดับ group และ project) และ environments |
| `runner` | สร้าง runner ของ group/project บนปลายทางด้วยค่าเดิม (tag, protected, timeout) และเก็บ token สำหรับลงทะเบียนเครื่องไว้ให้ |
| `settings` | ค่าตั้งของ project เช่น merge options, CI config path, timeout, features และสถานะ archive |
| `extras` | pipeline schedules (สร้างแบบปิดไว้ก่อน), webhooks, deploy keys และ members |

ใส่ได้หลายค่าโดยคั่นด้วยเครื่องหมายจุลภาค เช่น `--only repo,env` ถ้าไม่ใส่จะย้ายทั้งหมด

### โครงสร้าง subgroup ที่ปลายทาง (`--layout`)

| ค่า | ผลลัพธ์ | ตัวอย่าง |
|---|---|---|
| `keep` (ค่าเริ่มต้น) | เหมือนต้นทางทุกชั้น | `team/svc/api` → `org/team/svc/api` |
| `auto` | ทำตามโครงสร้างของ project ที่อยู่ปลายทางแล้ว (รวมถึงความผิดพลาดจากการย้ายครั้งก่อน) | — |
| `flat` | ตัด subgroup ออก | `team/svc/api` → `org/team/api` |
| `join` | รวมชื่อ subgroup เข้ากับชื่อ project | `team/svc/api` → `org/team/svc-api` |

ถ้าตัด subgroup ออกแล้วชื่อ project ชนกัน ระบบจะเปลี่ยนไปใช้ชื่อแบบรวม subgroup ให้อัตโนมัติ เช่น `svc-api`

### ตัวเลือกที่ใช้บ่อย

| ตัวเลือก | ความหมาย |
|---|---|
| `--dry-run` | แสดงแผนอย่างเดียว ไม่เปลี่ยนแปลงอะไร |
| `--relocate` | ย้าย project ที่อยู่ปลายทางแล้วให้เข้าโครงสร้างที่เลือก (ใช้ Transfer ของ GitLab, history ครบ) |
| `--include PATH` / `--exclude PATH` | ย้ายเฉพาะหรือข้ามบาง subgroup/project ระบุซ้ำได้ |
| `--with-parent-vars` | คัดลอกตัวแปรที่ต้นทางสืบทอดมาจาก group แม่ไปด้วย |
| `--rewrite-urls` | แปลง URL ของ repository ต้นทางที่อยู่ในค่าตัวแปรให้เป็น URL ปลายทาง |
| `--overwrite` | เขียนทับตัวแปรหรือกฎที่ปลายทางมีค่าต่างจากต้นทาง |
| `--force-push` | เขียนทับ branch ปลายทางที่ไม่ตรงกับต้นทาง (ย้อนกลับไม่ได้ ปิดไว้เป็นค่าเริ่มต้น) |
| `--activate-schedules` | เปิดใช้ pipeline schedule ทันที (ปกติสร้างแบบปิดไว้ เพื่อไม่ให้รันซ้อนกับระบบเดิม) |
| `-y` | ไม่ต้องถามยืนยัน |
| `--jobs N` | จำนวน project ที่ทำพร้อมกัน (ค่าเริ่มต้น 4) |
| `--lang th` / `--no-color` | ภาษา / ปิดสี |

## อัปเดตปลายทางให้ตามต้นทาง (sync)

ถ้าทีมยังทำงานบน GitLab เดิม ให้ย้ายครั้งแรกด้วย `group` หรือ `project` แล้วทำงานตามปกติ เมื่อต้องการให้ GitLab ใหม่ตามให้ทัน ให้สั่ง `sync` เมื่อไรก็ได้

```bash
glab-teleport sync                          # เลือกจากรายการที่เคยย้ายไว้
glab-teleport sync payments                 # ทั้ง group — จำปลายทางจากครั้งที่ย้ายไว้
glab-teleport sync payments/api             # เฉพาะ project เดียว
glab-teleport sync payments --dry-run       # ดูว่าจะมีอะไรเปลี่ยนบ้าง
glab-teleport sync payments --prune         # ลบสิ่งที่ต้นทางลบไปแล้วด้วย
```

- **ยึดต้นทางเป็นหลัก:** commit, branch, tag, project และ subgroup ใหม่จะถูกเพิ่ม ส่วนตัวแปร กฎ protected, environments และค่าตั้งที่เปลี่ยนจะถูกอัปเดตตามต้นทาง
- **รวดเร็ว:** project ที่ code ตรงกันอยู่แล้วจะไม่ถูกดาวน์โหลด หน้าจอแสดงเฉพาะ project ที่มีการเปลี่ยนแปลง
- **จำค่าที่เคยเลือก:** ปลายทาง โครงสร้าง และตัวเลือกอย่าง `--rewrite-urls` ใช้ตามครั้งที่ย้ายไว้ ไม่ต้องพิมพ์ซ้ำ
- **ไม่ลบหรือทับโดยไม่ตั้งใจ:**
  - สิ่งที่ต้นทางลบไปแล้วจะถูกลบที่ปลายทางก็ต่อเมื่อใส่ `--prune`
  - branch ที่มีคนแก้ที่ปลายทางจะถูกรายงาน ไม่เขียนทับ (เว้นแต่ใส่ `--force-push`)
  - ถ้าต้องการเพิ่มเฉพาะสิ่งที่ยังไม่มีโดยไม่แก้ค่าเดิม ใช้ `--no-overwrite`
- **ไม่สั่ง CI รัน:** push ด้วย `ci.skip` การ sync จึงไม่ทำให้เกิด build หรือ deploy ที่ปลายทาง

## หลักการทำงาน

1. **วางแผน:**
   - อ่านข้อมูลทั้งสองฝั่ง และเทียบ SHA ของทุก branch และ tag
   - กำหนดสถานะให้ project ต้นทางทุกตัว: `ตรงกันแล้ว`, `อัปเดต`, `สร้างใหม่`, `ย้ายที่` หรือ `ข้าม`
   - project ที่อยู่ระหว่างรอลบจะถูกข้ามอัตโนมัติ
2. **ย้าย:**
   - สร้าง group ก่อน แล้วย้าย project พร้อมกันหลายรายการ
   - ข้อมูล git ผ่าน bare repository ชั่วคราวในเครื่องของคุณ แล้วอ่าน SHA จากปลายทางกลับมายืนยันหลัง push
   - ข้อมูลอื่นผ่าน API สร้างเฉพาะรายการที่ยังไม่มี จึงรันซ้ำได้อย่างปลอดภัย
3. **ตรวจสอบ:** เทียบทุกอย่างที่ย้ายอีกครั้งแบบอ่านอย่างเดียว
4. **รายงาน:**
   - สรุปผลบนหน้าจอ พร้อม `report.md` และ `report.json` ที่ `~/.glab-teleport/runs/`
   - token สำหรับลงทะเบียน runner บันทึกไว้ใน `runner-tokens.txt` ข้างรายงาน (สิทธิ์ไฟล์ 600)

เมื่อเลือกตัด subgroup ออก ระบบจะใส่ CI/CD variables ของ subgroup นั้น**ไว้เฉพาะ project ที่เคยอยู่ใต้ subgroup นั้น** ไม่กระจายไปทั้ง group เพื่อไม่ให้ secret ของทีมหนึ่งรั่วไปถึง project ของทีมอื่น

### อ่านรายงาน

`report.md` เปิดอ่านได้ทั้งบน GitLab, GitHub และ editor ทั่วไป ประกอบด้วย:

- **สรุป:** จำนวน project, branch, tag, กฎ protected, CI/CD variables และ environments ของต้นทางเทียบกับปลายทาง
- **CI/CD variables แยกตาม environment scope:** จำนวนของแต่ละ scope ทั้งสองฝั่ง
- **รายการที่ต้องตรวจสอบ:** เช่น ตัวแปรที่ต้นทางซ่อนค่าไว้ หรือ branch ที่ปลายทางมี commit ใหม่กว่า
- **ตารางราย project:** ตัวเลขแสดงเป็น ปลายทาง / ต้นทาง
- **รายละเอียด:** ผลของแต่ละขั้นตอน และตารางตัวแปรทีละ key และ scope (ไม่มีค่าจริงของตัวแปร)

ดูรายงานย้อนหลังได้ด้วย `glab-teleport report` และ `glab-teleport report latest`

## ความปลอดภัยและความเป็นส่วนตัว

- **ตรวจแผนก่อนเสมอ:** ไม่มีการเปลี่ยนแปลงใดๆ จนกว่าคุณจะยืนยันแผน และ `--dry-run` จะไม่เขียนอะไรเลย
- **ไม่สั่ง CI รันเอง:** push branch ด้วย `ci.skip` จึงไม่มี pipeline (build หรือ deploy) เกิดขึ้นที่ปลายทางระหว่างย้าย และ pipeline schedule จะถูกสร้างแบบปิดไว้
- **ไม่ทับงานที่มีอยู่:**
  - ไม่เขียนทับ code ปลายทางที่ไม่เกี่ยวข้องกัน
  - branch ที่ปลายทางมี commit ใหม่กว่าจะถูกรายงานแทนการ force push
  - ตัวแปรที่ค่าต่างกันจะถูกเก็บไว้ตามเดิม จนกว่าจะสั่ง `--overwrite`
- **ไม่แสดงค่าลับ:** ค่าของตัวแปรเทียบกันในหน่วยความจำเท่านั้น ไม่แสดงบนหน้าจอและไม่เขียนลงรายงาน
- **การเก็บข้อมูลเข้าสู่ระบบ:** เก็บแยกตาม host ที่ `~/.config/glab-teleport/credentials.json` (สิทธิ์ไฟล์ 600) และข้อความ error จะถูกลบ token ออกก่อนแสดง
- **ใช้ใน CI:** ตั้งค่าผ่าน `GLAB_TELEPORT_SOURCE_URL`, `GLAB_TELEPORT_TARGET_URL`, `GLAB_TELEPORT_SOURCE_TOKEN` และ `GLAB_TELEPORT_TARGET_TOKEN`

## สิ่งที่ย้ายไม่ได้

GitLab API ไม่เปิดให้อ่านค่าลับหรือสร้างรายการเหล่านี้ขึ้นใหม่:

- issues และ merge requests
- container registry images และ packages
- ประวัติการรัน CI job
- deploy tokens และ access tokens

ถ้าต้องการย้าย issues และ merge requests ให้ใช้ *project export/import* ของ GitLab ส่วน CI/CD variables ที่ต้นทางซ่อนค่าไว้ (hidden) จะถูกระบุในรายงานเพื่อให้ตั้งค่าเองที่ปลายทาง

## งานหลังย้ายที่ควรทำ

```bash
glab-teleport refs              # ค้นหาไฟล์และตัวแปรที่ยังอ้างถึง GitLab ต้นทาง พร้อมวิธีแก้ที่แนะนำ
glab-teleport repoint           # สร้าง repoint.sh ส่งให้ทีมใช้เปลี่ยน git remote ในเครื่อง
```

ทีมรันสคริปต์ `./repoint.sh ~/code` เพื่อดูว่าจะเปลี่ยน remote อะไรบ้าง และ `./repoint.sh --apply ~/code` เพื่อเปลี่ยนจริง สคริปต์จะคง https เป็น https และ ssh เป็น ssh ส่วน repository ที่ยังไม่ได้ย้ายจะถูกข้าม

## คำถามที่พบบ่อย

**รันซ้ำได้หรือไม่**
ได้ การรันซ้ำจะทำเฉพาะส่วนที่ยังไม่ครบ project ที่ตรงกันแล้วจะไม่ถูกแตะ ระบบ push เฉพาะ ref ที่ขาด และสร้างเฉพาะตัวแปรที่ยังไม่มี

**ปลายทางมีบาง project อยู่แล้วแต่โครงสร้างไม่เหมือนต้นทาง**
ระบบจะพบ project เหล่านั้นจาก commit และอัปเดตที่ตำแหน่งเดิม ถ้าต้องการจัดให้เป็นโครงสร้างเดียวกับต้นทาง ใช้ `--layout keep --relocate`

**มีคนทำงานต่อที่ปลายทางไปแล้ว การย้ายซ้ำจะทับงานหรือไม่**
ไม่ทับ branch ที่ปลายทางมี commit ใหม่กว่าจะถูกรายงานว่า "อัปเดตไม่ได้" จะเขียนทับก็ต่อเมื่อสั่ง `--force-push` เท่านั้น

**ใช้ย้ายภายใน GitLab เครื่องเดียวกัน หรือระหว่าง namespace บน gitlab.com ได้หรือไม่**
ได้ แต่ถ้าย้ายภายในเครื่องเดียวกัน คำสั่ง *Transfer* ของ GitLab จะเร็วกว่า

**token หมดอายุระหว่างย้ายจะเป็นอย่างไร**
ถ้าใช้ OAuth ระบบจะต่ออายุเองอัตโนมัติ ถ้าใช้ personal access token ให้ตรวจวันหมดอายุด้วย `glab-teleport doctor` ก่อนเริ่มงานใหญ่

**ไฟล์รายงานและ log อยู่ที่ไหน**
อยู่ที่ `~/.glab-teleport/` เปลี่ยนตำแหน่งได้ด้วย `GLAB_TELEPORT_HOME` ดูประวัติทั้งหมดได้ด้วย `glab-teleport report`

## สำหรับนักพัฒนา

```bash
git clone https://github.com/Aekawan/glab-teleport && cd glab-teleport
python3 -m unittest discover -s tests      # ไม่ต้องติดตั้งไลบรารีเพิ่ม
node bin/glab-teleport.js --help
```

โค้ดเป็น Python 3.9+ standard library ล้วนๆ อยู่ใน `lib/glab_teleport/` (CLI, การวางแผน, การย้าย, การตรวจสอบ และรายงาน) พร้อมตัวเรียกขนาดเล็กสำหรับ npm ข้อความที่แสดงต่อผู้ใช้เขียนเป็น `t("English", "ไทย")` ไว้ตรงจุดที่ใช้งาน

## สัญญาอนุญาต

MIT ดูรายละเอียดใน [LICENSE](LICENSE)
