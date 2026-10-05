// The exact decision OpenClaw's sqlite-wal makes on Windows
// (openclaw/dist/sqlite-wal-*.js, resolvePathJournalPolicy, lines 124-132).
// "rollback" means journal_mode=DELETE, which is the mode that loses an
// acknowledged commit on a pull unless the filesystem syncs the folder.
import fs from "node:fs";
import path from "node:path";

const target = process.argv[2];
const UNC_A = new RegExp("^\\\\\\\\\\?\\\\UNC\\\\[^\\\\]+\\\\[^\\\\]+", "i");
const UNC_B = new RegExp("^\\\\\\\\(?![?.]\\\\)[^\\\\]+\\\\[^\\\\]+");
const DRIVE_A = new RegExp("^[A-Za-z]:[\\\\/]");
const DRIVE_B = new RegExp("^\\\\\\\\\\?\\\\[A-Za-z]:[\\\\/]", "i");

const isUnc = (p) => UNC_A.test(p) || UNC_B.test(p);
const isDrive = (p) => DRIVE_A.test(p) || DRIVE_B.test(p);

const norm = path.win32.normalize(target);
console.log("target:              ", JSON.stringify(target));
console.log("isWindowsUncPath:    ", isUnc(norm));
console.log("isWindowsDrivePath:  ", isDrive(norm));

let policy = "wal";
if (isUnc(norm)) {
  policy = "rollback";
} else if (isDrive(norm)) {
  try {
    const real = fs.realpathSync.native(target);
    console.log("realpathSync.native: ", JSON.stringify(real));
    policy = isUnc(path.win32.normalize(real)) ? "rollback" : "wal";
  } catch (error) {
    console.log("realpathSync.native THREW:", error.code, error.message);
    policy = "rollback";
  }
}
console.log("=> journal policy:   ", policy, policy === "wal" ? "(safe)" : "(DELETE - the exposed mode)");

try {
  console.log("statfsSync.type:     ", fs.statfsSync(target).type);
} catch (error) {
  console.log("statfsSync threw:    ", error.code);
}
