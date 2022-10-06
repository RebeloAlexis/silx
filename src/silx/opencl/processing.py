#!/usr/bin/env python
#
#    Project: S I L X project
#             https://github.com/silx-kit/silx
#
#    Copyright (C) 2012-2018 European Synchrotron Radiation Facility, Grenoble, France
#
#    Principal author:       Jérôme Kieffer (Jerome.Kieffer@ESRF.eu)
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#

"""
Common OpenCL abstract base classe for different processing
"""

__author__ = "Jerome Kieffer"
__contact__ = "Jerome.Kieffer@ESRF.eu"
__license__ = "MIT"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__date__ = "06/10/2022"
__status__ = "stable"

import sys
import os
import logging
import gc
from collections import namedtuple, OrderedDict
import numpy
import threading
from .common import ocl, pyopencl, release_cl_buffers, query_kernel_info, allocate_texture, check_textures_availability
from .utils import concatenate_cl_kernel
import platform

BufferDescription = namedtuple("BufferDescription", ["name", "size", "dtype", "flags"])
EventDescription = namedtuple("EventDescription", ["name", "event"])  # Deprecated, please use ProfileDescsription
ProfileDescsription = namedtuple("ProfileDescsription", ["name", "start", "stop"])

logger = logging.getLogger(__name__)


class KernelContainer(object):
    """Those object holds a copy of all kernels accessible as attributes"""

    def __init__(self, program):
        """Constructor of the class

        :param program: the OpenCL program as generated by PyOpenCL
        """
        self._program = program
        for kernel in program.all_kernels():
            self.__setattr__(kernel.function_name, kernel)

    def get_kernels(self):
        "return the dictionary with all kernels"
        return dict(item for item in self.__dict__.items()
                    if not item[0].startswith("_"))

    def get_kernel(self, name):
        "get a kernel from its name"
        logger.debug("KernelContainer.get_kernel(%s)", name)
        return self.__dict__.get(name)

    def max_workgroup_size(self, kernel_name):
        "Retrieve the compile time WORK_GROUP_SIZE for a given kernel"
        if isinstance(kernel_name, pyopencl.Kernel):
            kernel = kernel_name
        else:
            kernel = self.get_kernel(kernel_name)

        return query_kernel_info(self._program, kernel, "WORK_GROUP_SIZE")

    def min_workgroup_size(self, kernel_name):
        "Retrieve the compile time PREFERRED_WORK_GROUP_SIZE_MULTIPLE for a given kernel"
        if isinstance(kernel_name, pyopencl.Kernel):
            kernel = kernel_name
        else:
            kernel = self.get_kernel(kernel_name)

        return query_kernel_info(self._program, kernel, "PREFERRED_WORK_GROUP_SIZE_MULTIPLE")


class OpenclProcessing(object):
    """Abstract class for different types of OpenCL processing.

    This class provides:
    * Generation of the context, queues, profiling mode
    * Additional function to allocate/free all buffers declared as static attributes of the class
    * Functions to compile kernels, cache them and clean them
    * helper functions to clone the object
    """
    # Example of how to create an output buffer of 10 floats
    buffers = [BufferDescription("output", 10, numpy.float32, None),
               ]
    # list of kernel source files to be concatenated before compilation of the program
    kernel_files = []

    def __init__(self, ctx=None, devicetype="all", platformid=None, deviceid=None,
                 block_size=None, memory=None, profile=False):
        """Constructor of the abstract OpenCL processing class

        :param ctx: actual working context, left to None for automatic
                    initialization from device type or platformid/deviceid
        :param devicetype: type of device, can be "CPU", "GPU", "ACC" or "ALL"
        :param platformid: integer with the platform_identifier, as given by clinfo
        :param deviceid: Integer with the device identifier, as given by clinfo
        :param block_size: preferred workgroup size, may vary depending on the
                            out come of the compilation
        :param memory: minimum memory available on device
        :param profile: switch on profiling to be able to profile at the kernel
                         level, store profiling elements (makes code slightly slower)
        """
        self.sem = threading.Semaphore()
        self._X87_VOLATILE = None
        self.profile = None
        self.events = []  # List with of EventDescription, kept for profiling
        self.cl_mem = {}  # dict with all buffer allocated
        self.cl_program = None  # The actual OpenCL program
        self.cl_kernel_args = {}  # dict with all kernel arguments
        self.queue = None
        if ctx:
            self.ctx = ctx
        else:
            self.ctx = ocl.create_context(devicetype=devicetype,
                                          platformid=platformid, deviceid=deviceid,
                                          memory=memory)
        device_name = self.ctx.devices[0].name.strip()
        platform_name = self.ctx.devices[0].platform.name.strip()
        platform = ocl.get_platform(platform_name)
        self.device = platform.get_device(device_name)
        self.cl_kernel_args = {}  # dict with all kernel arguments

        self.set_profiling(profile)
        self.block_size = block_size
        self.program = None
        self.kernels = None

    def check_textures_availability(self):
        return check_textures_availability(self.ctx)

    def __del__(self):
        """Destructor: release all buffers and programs
        """
        try:
            self.reset_log()
            self.free_kernels()
            self.free_buffers()
            if self.queue is not None:
                self.queue.finish()
        except Exception as err:
            logger.warning("%s: %s", type(err), err)
        self.queue = None
        self.device = None
        self.ctx = None
        gc.collect()

    def allocate_buffers(self, buffers=None, use_array=False):
        """
        Allocate OpenCL buffers required for a specific configuration

        :param buffers: a list of BufferDescriptions, leave to None for
                        paramatrized buffers.
        :param use_array: allocate memory as pyopencl.array.Array
                            instead of pyopencl.Buffer

        Note that an OpenCL context also requires some memory, as well
        as Event and other OpenCL functionalities which cannot and are
        not taken into account here.  The memory required by a context
        varies depending on the device. Typical for GTX580 is 65Mb but
        for a 9300m is ~15Mb In addition, a GPU will always have at
        least 3-5Mb of memory in use.  Unfortunately, OpenCL does NOT
        have a built-in way to check the actual free memory on a
        device, only the total memory.
        """
        if buffers is None:
            buffers = self.buffers

        with self.sem:
            mem = {}

            # check if enough memory is available on the device
            ualloc = 0
            for buf in buffers:
                ualloc += numpy.dtype(buf.dtype).itemsize * numpy.prod(buf.size)
            logger.info("%.3fMB are needed on device: %s,  which has %.3fMB",
                        ualloc / 1.0e6, self.device, self.device.memory / 1.0e6)

            if ualloc >= self.device.memory:
                raise MemoryError("Fatal error in allocate_buffers. Not enough "
                                  " device memory for buffers (%lu requested, %lu available)"
                                  % (ualloc, self.device.memory))

            # do the allocation
            try:
                if use_array:
                    for buf in buffers:
                        mem[buf.name] = pyopencl.array.empty(self.queue, buf.size, buf.dtype)
                else:
                    for buf in buffers:
                        size = numpy.dtype(buf.dtype).itemsize * numpy.prod(buf.size)
                        mem[buf.name] = pyopencl.Buffer(self.ctx, buf.flags, int(size))
            except pyopencl.MemoryError as error:
                release_cl_buffers(mem)
                raise MemoryError(error)

        self.cl_mem.update(mem)

    def add_to_cl_mem(self, parrays):
        """
        Add pyopencl.array, which are allocated by pyopencl, to self.cl_mem.
        This should be used before calling allocate_buffers().

        :param parrays: a dictionary of `pyopencl.array.Array` or `pyopencl.Buffer`
        """
        mem = self.cl_mem
        for name, parr in parrays.items():
            mem[name] = parr
        self.cl_mem.update(mem)

    def check_workgroup_size(self, kernel_name):
        "Calculate the maximum workgroup size from given kernel after compilation"
        return self.kernels.max_workgroup_size(kernel_name)

    def free_buffers(self):
        """free all device.memory allocated on the device
        """
        with self.sem:
            for key, buf in list(self.cl_mem.items()):
                if buf is not None:
                    if isinstance(buf, pyopencl.array.Array):
                        try:
                            buf.data.release()
                        except pyopencl.LogicError:
                            logger.error("Error while freeing buffer %s", key)
                    else:
                        try:
                            buf.release()
                        except pyopencl.LogicError:
                            logger.error("Error while freeing buffer %s", key)
                    self.cl_mem[key] = None

    def compile_kernels(self, kernel_files=None, compile_options=None):
        """Call the OpenCL compiler

        :param kernel_files: list of path to the kernel
            (by default use the one declared in the class)
        :param compile_options: string of compile options
        """
        # concatenate all needed source files into a single openCL module
        kernel_files = kernel_files or self.kernel_files
        kernel_src = concatenate_cl_kernel(kernel_files)

        compile_options = compile_options or self.get_compiler_options()
        logger.info("Compiling file %s with options %s", kernel_files, compile_options)
        try:
            self.program = pyopencl.Program(self.ctx, kernel_src).build(options=compile_options)
        except (pyopencl.MemoryError, pyopencl.LogicError) as error:
            raise MemoryError(error)
        else:
            self.kernels = KernelContainer(self.program)

    def free_kernels(self):
        """Free all kernels
        """
        for kernel in self.cl_kernel_args:
            self.cl_kernel_args[kernel] = []
        self.kernels = None
        self.program = None

# Methods about Profiling
    def set_profiling(self, value=True):
        """Switch On/Off the profiling flag of the command queue to allow debugging

        :param value: set to True to enable profiling, or to False to disable it.
                      Without profiling, the processing is marginally faster

        Profiling information can then be retrieved with the 'log_profile' method
        """
        if bool(value) != self.profile:
            with self.sem:
                self.profile = bool(value)
                if self.queue is not None:
                    self.queue.finish()
                if self.profile:
                    self.queue = pyopencl.CommandQueue(self.ctx,
                        properties=pyopencl.command_queue_properties.PROFILING_ENABLE)
                else:
                    self.queue = pyopencl.CommandQueue(self.ctx)

    def profile_add(self, event, desc):
        """
        Add an OpenCL event to the events lists, if profiling is enabled.

        :param event: pyopencl.NanyEvent.
        :param desc: event description
        """
        if self.profile:
            try:
                profile = event.profile
                self.events.append(ProfileDescsription(desc, profile.start, profile.end))
            except Exception:
                # Probably the driver does not support profiling
                pass

    def profile_multi(self, event_lists):
        """
        Extract profiling info from several OpenCL event, if profiling is enabled.

        :param event_lists: list of ("desc", pyopencl.NanyEvent).
        """
        if self.profile:
            for event_desc in event_lists:
                if isinstance(event_desc, ProfileDescsription):
                    self.events.append(event_desc)
                else:
                    if isinstance(event_desc, EventDescription):
                        desc, event = event_desc
                    else:
                        desc = "?"
                        event = event_desc
                    try:
                        profile = event.profile
                        start = profile.start
                        end = profile.end
                    except Exception:
                        # probably an unfinished job ... use old-style.
                        self.events.append(event_desc)
                    else:
                        self.events.append(ProfileDescsription(desc, start, end))

    def log_profile(self, stats=False):
        """If we are in profiling mode, prints out all timing for every single OpenCL call
        
        :param stats: if True, prints the statistics on each kernel instead of all execution timings
        :return: list of lines to print
        """
        total_time = 0.0
        out = [""]
        if stats:
            stats = OrderedDict()
            out.append(f"OpenCL kernel profiling statistics in milliseconds for: {self.__class__.__name__}")
            out.append(f"{'Kernel name':>50} (count):      min   median      max     mean      std")
        else:
            stats = None
            out.append(f"Profiling info for OpenCL: {self.__class__.__name__}")

        if self.profile:
            for e in self.events:
                if isinstance(e, ProfileDescsription):
                    name = e[0]
                    t0 = e[1]
                    t1 = e[2]
                elif isinstance(e, EventDescription):
                    name = e[0]
                    pr = e[1].profile
                    t0 = pr.start
                    t1 = pr.end
                else:
                    name = "?"
                    t0 = e.profile.start
                    t1 = e.profile.end

                et = 1e-6 * (t1 - t0)
                total_time += et
                if stats is None:
                    out.append(f"{name:>50}        : {et:.3f}ms")
                else:
                    if name in stats:
                        stats[name].append(et)
                    else:
                        stats[name] = [et]
            if stats is not None:
                for k, v in stats.items():
                    n = numpy.array(v)
                    out.append(f"{k:>50} ({len(v):5}): {n.min():8.3f} {numpy.median(n):8.3f} {n.max():8.3f} {n.mean():8.3f} {n.std():8.3f}")
            out.append("_" * 80)
            out.append(f"{'Total OpenCL execution time':>50}        : {total_time:.3f}ms")

        logger.info(os.linesep.join(out))
        return out

    def reset_log(self):
        """
        Resets the profiling timers
        """
        with self.sem:
            self.events = []

# Methods about textures
    def allocate_texture(self, shape, hostbuf=None, support_1D=False):
        return allocate_texture(self.ctx, shape, hostbuf=hostbuf, support_1D=support_1D)

    def transfer_to_texture(self, arr, tex_ref):
        """
        Transfer an array to a texture.

        :param arr: Input array. Can be a numpy array or a pyopencl array.
        :param tex_ref: texture reference (pyopencl._cl.Image).
        """
        copy_args = [self.queue, tex_ref, arr]
        shp = arr.shape
        ndim = arr.ndim
        if ndim == 1:
            # pyopencl and OpenCL < 1.2 do not support image1d_t
            # force 2D with one row in this case
            # ~ ndim = 2
            shp = (1,) + shp
        copy_kwargs = {"origin":(0,) * ndim, "region": shp[::-1]}
        if not(isinstance(arr, numpy.ndarray)):  # assuming pyopencl.array.Array
            # D->D copy
            copy_args[2] = arr.data
            copy_kwargs["offset"] = 0
        ev = pyopencl.enqueue_copy(*copy_args, **copy_kwargs)
        self.profile_add(ev, "Transfer to texture")

    @property
    def x87_volatile_option(self):
        # this is running 32 bits OpenCL woth POCL
        if self._X87_VOLATILE is None:
            if (platform.machine() in ("i386", "i686", "x86_64", "AMD64") and
                    (tuple.__itemsize__ == 4) and
                    self.ctx.devices[0].platform.name == 'Portable Computing Language'):
                self._X87_VOLATILE = "-DX87_VOLATILE=volatile"
            else:
                self._X87_VOLATILE = ""
        return self._X87_VOLATILE

    def get_compiler_options(self, x87_volatile=False):
        """Provide the default OpenCL compiler options

        :param x87_volatile: needed for Kahan summation
        :return: string with compiler option
        """
        option_list = []
        if x87_volatile:
            option_list.append(self.x87_volatile_option)
        return " ".join(i for i in option_list if i)

# This should be implemented by concrete class
#     def __copy__(self):
#         """Shallow copy of the object
#
#         :return: copy of the object
#         """
#         return self.__class__((self._data, self._indices, self._indptr),
#                               self.size, block_size=self.BLOCK_SIZE,
#                               platformid=self.platform.id,
#                               deviceid=self.device.id,
#                               checksum=self.on_device.get("data"),
#                               profile=self.profile, empty=self.empty)
#
#     def __deepcopy__(self, memo=None):
#         """deep copy of the object
#
#         :return: deepcopy of the object
#         """
#         if memo is None:
#             memo = {}
#         new_csr = self._data.copy(), self._indices.copy(), self._indptr.copy()
#         memo[id(self._data)] = new_csr[0]
#         memo[id(self._indices)] = new_csr[1]
#         memo[id(self._indptr)] = new_csr[2]
#         new_obj = self.__class__(new_csr, self.size,
#                                  block_size=self.BLOCK_SIZE,
#                                  platformid=self.platform.id,
#                                  deviceid=self.device.id,
#                                  checksum=self.on_device.get("data"),
#                                  profile=self.profile, empty=self.empty)
#         memo[id(self)] = new_obj
#         return new_obj
