# bindiff.py - BinDiff wrapper script for multiple binary diffing
# Takahiro Haruyama (@cci_forensics)

import argparse, subprocess, os, sqlite3, time, pickle, re, multiprocessing, sys, struct, logging
from multiprocessing.process import current_process
from prettytable import PrettyTable
import pefile
from macholib.MachO import MachO
from macholib.mach_o import *
from elftools.elf.elffile import ELFFile
import idb

logging.basicConfig(level=logging.ERROR) # to suppress python-idb warning

# paths (should be edited)
g_out_dir = r'E:\CodeProt\spec_test\32_server\run-spec-result-1.0\bindiff_db' 
g_ida_dir = r'C:\Program Files\IDA 7.2'
g_exp_path = r'E:\CodeProt\spec_test\bindiff-wrapper\bindiff_export.idc'
g_differ_path = r"C:\Program Files\BinDiff\bin\bindiff.exe"
#g_differ_path = r'C:\Program Files (x86)\zynamics\BinDiff 4.2\bin\differ64.exe'
g_save_fname_path = r'E:\CodeProt\spec_test\32_server\run-spec-result-1.0\bindiff_func_name\save_func_names.py'

# parameters
g_ws_th = 0.01 # whole binary similarity threshold
g_fs_th = 1.0 #0.70 # function similarity threshold
g_ins_th = 10 # instruction threshold
g_bb_th = 0 # basic block threshold
g_size_th = 100 #10 # file size threshold (MB)
#g_func_regex = r'sub_|fn_|chg_' # function name filter rule
g_func_regex = r'.*' # function name filter rule

class LocalError(Exception): pass
class ProcExportError(LocalError): pass
class ProcDiffError(LocalError): pass
class LoadFuncNamesError(LocalError): pass
class FileNotFoundError(LocalError): pass
class ChildProcessError(LocalError): pass
class GenIdaFileError(LocalError): pass

class BinDiff(object):
    
    def __init__ (self, primary, out_dir, ws_th, fs_th, ins_th, bb_th, size_th, func_regex, debug=False, clear=False, newidb=False, use_pyidb=False):
    #def __init__ (self, primary, out_dir, ws_th, fs_th, ins_th, bb_th, size_th, debug=False, clear=False, noidb=False, use_pyidb=False):        
        self._debug = debug
        self._clear = clear
        self._newidb = newidb
        self._lock = multiprocessing.Lock()        
        self._primary = primary
        self._ws_th = ws_th
        self._fs_th = fs_th
        self._ins_th = ins_th
        self._bb_th = bb_th
        self._size_th = size_th
        self._out_dir = out_dir
        self.use_pyidb = use_pyidb
        
        self._format, self._arch = self._get_machine_type(primary)
        if self._format is None:
            raise ProcExportError('primary binary should be PE/Mach-O/ELF'.format(primary))
        self._dprint('primary binary format: {}'.format(self._format))
        self._dprint('primary binary architecture: {}'.format(self._arch))
        
        self._ida_path = self._get_ida_path(self._arch)
        res = self._files_not_found()
        if res is not None:
            raise FileNotFoundError('file is not found: {}'.format(res))
        self._dprint('IDA binary path for primary: {}'.format(self._ida_path))
        
        if self._make_BinExport(self._primary, self._ida_path) != 0:
            raise ProcExportError('primary BinExport failed: {}'.format(primary))

        if self.use_pyidb:
            idb_path = self._get_idb_path(primary, self._arch)
            if not os.path.exists(idb_path):
                self._gen_ida_file(self._primary)
            self._func_names = self._load_func_names_pyidb(idb_path)
        else:
            self._func_p = re.compile(func_regex)
            self._func_regex = func_regex
            self._func_names = self._load_func_names_default(func_regex, primary,
                                                             self._ida_path)
        
        self._high_ws = {}
        self._high_fs = {}
        self._diff_cnt = 0
        #self._dprint('Exiting __init__ function!')

    def _dprint(self, msg):
        if self._debug:
            self._lock.acquire()            
            print('[+] [{}]: {}'.format(os.getpid(), msg))
            self._lock.release()

    def _get_machine_type(self, path):
        try:
            pe = pefile.PE(path)
            format_ = 'PE'
            if pefile.MACHINE_TYPE[pe.FILE_HEADER.Machine].find('I386') != -1:
                arch = '32-bit'
            else:
                arch = '64-bit'
        except (pefile.PEFormatError,KeyError) as detail:
            try:
                self._dprint(detail)
                m = MachO(path)
                format_ = 'Mach-O'
                for header in m.headers:
                    if CPU_TYPE_NAMES.get(header.header.cputype,header.header.cputype) == 'x86_64':
                    #if header.MH_MAGIC == MH_MAGIC_64:
                        arch = '64-bit'
                    else:
                        arch = '32-bit'
            except:
                try:
                    elffile = ELFFile(open(path, 'rb'))
                    format_ = 'ELF'
                    e_ident = elffile.header['e_ident']
                    if e_ident['EI_CLASS'] == 'ELFCLASS64':
                        arch = '64-bit'
                    else:
                        arch = '32-bit'
                except:                    
                    return None, None
                    #format_ = 'shellcode'
                    #arch = '32-bit' # 32-bit fixed
        return format_, arch

    def _files_not_found(self):
        #for path in (self._ida_path, g_exp_path, g_save_fname_path, g_differ_path):
        for path in (self._ida_path, g_exp_path, g_differ_path):
            if not os.path.isfile(path):
                return path
        return None

    def _get_db_path_noext(self, target):
        return os.path.join(self._out_dir, os.path.splitext(os.path.basename(target))[0])
        #return os.path.join(self._out_dir, os.path.basename(target))
    
    def _get_db_path_withext(self, target):
        return os.path.join(self._out_dir, os.path.basename(target))

    #get the path of .idb or .i64 file corresponding to target
    def _get_idb_path(self, target, arch):
        db_ext = '.idb' if arch == '32-bit' else '.i64'
        target_split = os.path.splitext(target)[0]
        
        if os.path.exists(target_split + db_ext):
            return target_split + db_ext
        else:
            return target + db_ext # for recent IDA versions

    #generate .ida or .i64 file for target
    def _gen_ida_file(self, target):
        format, arch = self._get_machine_type(target)
        ida_path = self._get_ida_path(arch)
        cmd = [ida_path, '-B', target]
        self._dprint('Generating .ida/.i64 file for {}'.format(target))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()            
        if proc.returncode != 0:
            raise GenIdaFileError('Generating .ida/.i64 file failed: {}'.format(target))

    def _get_ida_path(self, arch):
        #idaq = 'idaq.exe' if arch == '32-bit' else 'idaq64.exe'
        idaq = 'ida.exe' if arch == '32-bit' else 'ida64.exe'
        return os.path.join(g_ida_dir, idaq)        

    def _load_func_names_pyidb(self, idb_path): # exlcude library/thunk functions
        pickle_path = os.path.splitext(os.path.join(self._out_dir, os.path.basename(idb_path)))[0] + '_func_names.pickle'
        if self._clear or not os.path.exists(pickle_path):        
            func_names = {}        
            with idb.from_file(idb_path) as db:
                api = idb.IDAPython(db)
                for ea in api.idautils.Functions(api.idc.MinEA(), api.idc.MaxEA()):
                    flags = api.idc.GetFunctionFlags(ea)
                    if flags & api.ida_funcs.FUNC_LIB or flags & api.ida_funcs.FUNC_THUNK:
                        continue
                    func_name = api.idc.GetFunctionName(ea)
                    func_names[ea] = func_name
            with open(pickle_path, 'wb') as f:
                pickle.dump(func_names, f)

        with open(pickle_path, 'rb') as f:
            self._dprint('function names loaded: {}'.format(idb_path))
            return pickle.load(f)
                        
    # default function without python-idb
    def _load_func_names_default(self, func_regex, path, ida_path):
        pickle_path = os.path.splitext(os.path.join(self._out_dir, os.path.basename(path)))[0] + '_func_names.pickle'
        if self._clear or not os.path.exists(pickle_path):
            cmd = [ida_path, '-A', '-S{}'.format(g_save_fname_path), '-Osave_func_names:{}:{}'.format(func_regex, pickle_path), path]

            self._dprint('saving function names for {}'.format(path))            
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = proc.communicate()            
            if proc.returncode != 0:
                raise LoadFuncNamesError('function names saving failed: {}'.format(path))
            
        with open(pickle_path, 'rb') as f:
            self._dprint('function names loaded: {}'.format(path))
            return pickle.load(f)
        
        raise LoadFuncNamesError('function names loading failed: {}'.format(path))

    def _make_BinExport(self, target, ida_path):
        #binexp_path = self._get_db_path_noext(target) + '.BinExport'
        binexp_path = self._get_db_path_withext(target) + '.BinExport'
        #binexp_path = os.path.splitext(target)[0] + '.BinExport'
        if not self._clear and os.path.exists(binexp_path):
            self._dprint('already existed BinExport: {}'.format(binexp_path))
            return 0

        #cmd = [ida_path, '-A', '-S{}'.format(g_exp_path), '-OExporterModule:{}'.format(binexp_path), target]  # the .BinExport filename should be specified in 4.3
        #if self._debug:
            #cmd = [ida_path, '-S{}'.format(g_exp_path), '-OBinExportModule:{}'.format(binexp_path), target]
        #else:
        cmd = [ida_path, '-A', '-S{}'.format(g_exp_path), '-OBinExportModule:{}'.format(binexp_path), target]
        #print(cmd)
        
        self._dprint('getting BinExport for {}'.format(target))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        self._dprint('[proc returncode {}'.format(proc.returncode))
        return proc.returncode

    def _get_BinDiff_path(self, secondary):
        primary_withext = self._get_db_path_withext(self._primary)
        secondary_withext = secondary
        return primary_withext + '_vs_' + os.path.basename(secondary_withext) + '.BinDiff'
        #primary_noext = self._get_db_path_noext(self._primary)
        #secondary_noext = os.path.splitext(secondary)[0]
        #return primary_noext + '_vs_' + os.path.basename(secondary_noext) + '.BinDiff'

    def _make_BinDiff(self, secondary):
        #print("makinf BinDiff: {} vs {}".format(self._primary, secondary));
        #pri_binexp = self._get_db_path_noext(self._primary) + '.BinExport'
        #sec_binexp = self._get_db_path_noext(secondary) + '.BinExport'
        pri_binexp = self._get_db_path_withext(self._primary) + '.BinExport'
        sec_binexp = self._get_db_path_withext(secondary) + '.BinExport'
        #pri_binexp = os.path.splitext(self._primary)[0] + '.BinExport'
        #sec_binexp = os.path.splitext(secondary)[0] + '.BinExport'
        bindiff_path = self._get_BinDiff_path(secondary)
        if not self._clear and os.path.exists(bindiff_path):
            self._dprint('already existed BinDiff: {}'.format(bindiff_path))
            return 0, None            
        
        cmd = [g_differ_path, '--primary={}'.format(pri_binexp), '--secondary={}'.format(sec_binexp), '--output_dir={}'.format(self._out_dir)]
        #print(cmd)
        
        self._dprint('diffing the binaries..')
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        self._dprint('differ output:')
        self._dprint(stdout)
        self._dprint(stderr)
        return proc.returncode, cmd

    def is_skipped(self, secondary):
        # file check (in case of the same dir)
        #if os.path.splitext(self._primary)[0] == os.path.splitext(secondary)[0]:
        if self._primary == secondary:
            return True
        
        # target at executables
        #if os.path.splitext(secondary)[1] in ('.BinExport', '.BinDiff', '.idb', '.i64'):
        if os.path.splitext(secondary)[1] in ('.BinExport', '.BinDiff', '.idb', '.i64', '.asm'):
            return True
        
        # size check
        if (os.path.getsize(secondary) >> 20) > self._size_th:
            self._dprint('The size is bigger (skipped): {}'.format(secondary))
            return True
        
        # format/arch check
        format_, arch = self._get_machine_type(secondary)
        if format_ is None:
            return True
        #elif format_ != self._format or arch != self._arch:
        elif format_ != self._format: # only check the format 
            self._dprint('different executable format (skipped): {}'.format(secondary))
            return True

        # skip if idb not found
        idb_path = self._get_idb_path(secondary, arch)
        if not self._newidb and not os.path.exists(idb_path):
            self._dprint('no existing idb (skipped): {}'.format(secondary))
            return True
        
        return False

    def check_similarity(self, secondary, q=None):
        format_, arch = self._get_machine_type(secondary)
        ida_path = self._get_ida_path(arch)
        self._dprint('IDA binary path for secondary: {}'.format(ida_path))        
        if self._make_BinExport(secondary, ida_path) != 0:
            if q is not None:
                q.put((None, None))            
            raise ProcExportError('secondary BinExport failed: {}'.format(secondary))

        retcode, cmd = self._make_BinDiff(secondary)
        if retcode != 0:
            if q is not None:
                q.put((None, None))            
            raise ProcDiffError('BinDiff failed: {}'.format(cmd))

        conn = sqlite3.connect(self._get_BinDiff_path(secondary))
        c = conn.cursor()
        try:
            c.execute("SELECT similarity,confidence FROM metadata")
        except sqlite3.OperationalError as detail:
            print('[!] .BinDiff database ({}) is something wrong: {}'.format(self._get_BinDiff_path(secondary), detail))
            return
            
        ws, wc = c.fetchone()
        self._dprint('whole binary similarity={} confidence={}'.format(ws, wc))
        c.execute("SELECT address1,address2,similarity,confidence FROM function WHERE similarity > ? and instructions > ? and basicblocks > ?", (self._fs_th, self._ins_th, self._bb_th))
        frows = c.fetchall()
        self._dprint('{} similar functions detected'.format(len(frows)))
        conn.close()

        c_high_ws = {}
        c_high_fs = {}
        if ws > self._ws_th:
            c_high_ws[secondary] = {'similarity':ws, 'confidence':wc}
        elif frows:
            if self.use_pyidb:
                idb_path = self._get_idb_path(secondary, arch)
                func_names = self._load_func_names_pyidb(idb_path)
            else:
                func_names = self._load_func_names_default(self._func_regex, secondary,
                                                           ida_path)
            for row in frows:
                addr1, addr2, fs, fc = row
                self._dprint('addr1={:#x}, addr2={:#x}, similarity={}, confidence={}'.format(addr1, addr2, fs, fc))
                if addr1 in self._func_names and addr2 in func_names:
                    c_high_fs[(addr1, self._func_names[addr1], addr2, func_names[addr2], secondary)] = {'similarity':fs, 'confidence':fc}
            if not c_high_fs and not self._debug:
                os.remove(self._get_BinDiff_path(secondary))
        else:
            if not self._debug:
                os.remove(self._get_BinDiff_path(secondary))

        #self._dprint(c_high_ws)
        #self._dprint(c_high_fs)
        if q is None:
            self._high_ws = c_high_ws
            self._high_fs = c_high_fs
        else:
            q.put((c_high_ws, c_high_fs))

    def check_similarities(self, secondary_dir, recursively):
        if recursively:
            seconds = [os.path.join(root, file_) for root, dirs, files in os.walk(secondary_dir) for file_ in files]
        else:
            seconds = [os.path.join(secondary_dir, entry) for entry in os.listdir(secondary_dir) if os.path.isfile(os.path.join(secondary_dir, entry))]

        procs = []            
        for secondary in seconds:
            if self.is_skipped(secondary):
                continue
            q = multiprocessing.Queue()
            p = multiprocessing.Process(target=self.check_similarity, args=(secondary, q))
            p.start()
            procs.append((p,q))
        self._diff_cnt = len(procs)
        for p,q in procs:
            c_high_ws, c_high_fs = q.get()
            self._high_ws.update(c_high_ws)
            self._high_fs.update(c_high_fs)
            p.join()

    def increment_count(self):
        self._diff_cnt += 1
    
    def get_result(self):
        return self._high_ws, self._high_fs, self._diff_cnt

def diff_sinlge_spec_benchmark(benchmark_dir, bin_type, out_dir, ws_th, fs_th, ins_th, bb_th, size_th, func_regex, debug=False, clear=False, newidb=False, use_pyidb=False):
    high_ws = high_fs = None
    exe_path = os.path.join(benchmark_dir, 'exe')
    
    if os.path.exists(exe_path):
        result_file_path = os.path.join(exe_path, "bindiff_result.txt")
        result_file = open(result_file_path, "a+")

        current_time = time.strftime('%Y_%m_%d %H:%M:%S',time.localtime(time.time()))
        result_file.write("[{}]Start processing benchmark: {}\n".format(current_time, benchmark_dir))
        print("[{}]Start processing benchmark: {}".format(current_time, benchmark_dir))
        print("Bindiff result is writed to file: {}".format(result_file_path))

        primary = ''
        for item in os.listdir(exe_path):
            if os.path.splitext(item)[-1] == '.origin':
                primary = os.path.join(exe_path, item)
        if primary == '':
            print('[!] can not find the .origin file in {}, ignored!}'.format(exe_path))
            #result_file.write('[!] can not find the .origin file in {}, ignored!}\r\n'.format(exe_path))
            return
        bd = BinDiff(primary, out_dir, ws_th, fs_th, ins_th, bb_th, size_th, func_regex, debug, clear, newidb, use_pyidb)
        for item in os.listdir(exe_path):
            if item == os.path.basename(primary):
                continue
            if bin_type != '*' and os.path.splitext(item)[-1] != bin_type:
                continue
            secondary = os.path.join(exe_path, item)
            
            start = time.time()
            try:
                if os.path.isfile(secondary):
                    if not bd.is_skipped(secondary):
                        current_time = time.strftime('%Y_%m_%d %H:%M:%S',time.localtime(time.time()))
                        print("[{}]Bindiffing {} vs {}".format(current_time, os.path.basename(primary), os.path.basename(secondary)))
                        bd.check_similarity(secondary)
                        bd.increment_count()
                    else:
                        #print("Skip diffing {} vs {}".format(primary, secondary))
                        continue
                else:
                    #print("{} is not a file, ignode!".format(secondary))
                    continue
                high_ws, high_fs, cnt = bd.get_result()
            except LocalError as e:
                print('[!] {} ({})'.format(str(e), type(e)))
                return 
            elapsed = time.time() - start
            
            result_file.write('---------------------------------------------\n')
            result_file.write('[*] BinDiff result:\n')
            result_file.write('[*] elapsed time = {} sec, number of diffing = {}\n'.format(elapsed, cnt))
            result_file.write('[*] primary binary: (({}))\n'.format(os.path.basename(primary)))
            if high_ws:
                result_file.write('\n============== {} high similar binaries (>{}) ================\n'.format(len(high_ws), ws_th))
                table = PrettyTable(['similarity', 'secondary binary'])
                for path,res in sorted(list(high_ws.items()), key=lambda x:x[1]['similarity'], reverse=True):
                    table.add_row([res['similarity'], '(({}))'.format(os.path.basename(path))])
                result_file.write(str(table))
            if high_fs:
                result_file.write('\n============== {} high similar functions (>{}), except high similar binaries ================'.format(len(high_fs), fs_th))
                table = PrettyTable(['similarity', 'primary addr', 'primary name', 'secondary addr', 'secondary name', 'secondary binary'])
                for key,res in sorted(list(high_fs.items()), key=lambda x:(x[1]['similarity'], x[0][0]), reverse=True):
                    addr1, func_name1, addr2, func_name2, path = key
                    table.add_row([res['similarity'], '{:#x}'.format(addr1), func_name1[:0x20], '{:#x}'.format(addr2), func_name2[:0x20], '{}'.format(os.path.basename(path))])
                result_file.write(str(table))
            if (not high_ws) and (not high_fs):
                result_file.write('\nno similar binaries/functions found\n')
            result_file.write('---------------------------------------------\n')
        current_time = time.strftime('%Y_%m_%d %H:%M:%S',time.localtime(time.time()))
        print("[{}]Processing benchmark finished: {}\n".format(current_time, benchmark_dir))
        result_file.write("[{}]Processing benchmark finished: {}\n\n\n".format(current_time, benchmark_dir))
        result_file.close()
    else:
        print("[!] Can not find {}, ignored!".format(benchmark_dir))

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    #parser.add_argument('primary', help="primary binary to compare")
    #parser.add_argument('spec_exes_dir', help="top directory of spec executables to bindiff")
    parser.add_argument('--out_dir', '-o', default=g_out_dir, help="output directory including .BinExport/.BinDiff")
    parser.add_argument('--ws_th', '-w', type=float, default=g_ws_th, help="whole binary similarity threshold")
    parser.add_argument('--fs_th', '-f', type=float, default=g_fs_th, help="function similarity threshold")
    parser.add_argument('--ins_th', '-i', type=int, default=g_ins_th, help="instruction threshold")
    parser.add_argument('--bb_th', '-b', type=int, default=g_bb_th, help="basic block threshold")    
    parser.add_argument('--size_th', '-s', type=int, default=g_size_th, help="file size threshold (MB)")
    parser.add_argument('--func_regex', '-e', default=g_func_regex, help="function name regex to include in the result")
    parser.add_argument('--debug', '-d', action='store_true', help="print debug output")
    parser.add_argument('--clear', '-c', action='store_true', help="clear .BinExport, .BinDiff and function name cache")
    parser.add_argument('--newidb', '-n', action='store_true', help="create an idb for the secondary binary")
    parser.add_argument('--use_pyidb', action='store_true', help="use python-idb")
    parser.add_argument('--type', '-t', type=str ,default="*", help='Specify the type of binary to bindiff')
    
    #subparsers = parser.add_subparsers(dest='mode', help='mode: 1, m')
    #parser_1 = subparsers.add_parser('1', help='BinDiff 1 to 1')
    #parser_1.add_argument('secondary', help="secondary binary to compare")    
    #parser_m = subparsers.add_parser('m', help='BinDiff 1 to many')
    #parser_m.add_argument('secondary_dir', help="secondary directory including binaries to compare")
    #parser_m.add_argument('--recursively', '-r', action='store_true', help="getting binaries recursively")

    subparsers = parser.add_subparsers(dest='mode', help='mode: 1, all')
    parser_1 = subparsers.add_parser('1', help='BinDiff single spec benchmark')
    parser_1.add_argument('single_benchmark_dir', help="directory of single benchmark to bindiff") 
    parser_m = subparsers.add_parser('all', help='BinDiff all spec benchmarks')
    parser_m.add_argument('all_benchmark_dir', help="top directory of benchmarks to bindiff")

    args = parser.parse_args()

    if args.mode == '1':
        diff_sinlge_spec_benchmark(args.single_benchmark_dir, args.type, args.out_dir, args.ws_th, args.fs_th, args.ins_th, args.bb_th, args.size_th, args.func_regex, args.debug, args.clear, args.newidb, args.use_pyidb)
    if args.mode == 'all':
        for item in os.listdir(args.all_benchmark_dir):
            single_benchmark_dir = os.path.join(args.all_benchmark_dir, item)
            if os.path.isdir(single_benchmark_dir):
                diff_sinlge_spec_benchmark(single_benchmark_dir, args.type, args.out_dir, args.ws_th, args.fs_th, args.ins_th, args.bb_th, args.size_th, args.func_regex, args.debug, args.clear, args.newidb, args.use_pyidb)
        
if ( __name__ == "__main__" ):
    main()
