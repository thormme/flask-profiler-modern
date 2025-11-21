use py_spy::timer::Timer;
use pyo3::prelude::*;
use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use py_spy::config::{Config, FileFormat, RecordDuration};
use py_spy::{Frame, StackTrace, sampler};
use anyhow::{Error, format_err};
use remoteprocess;
use console::{style};
use chrono::{Local, SecondsFormat};
use core::sync::{atomic::Ordering};
use std::time::Duration;
use log::warn;
use std::{mem, thread};

use std::io::{Read, Write};
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
mod speedscope;

pub trait Recorder {
    fn increment(&mut self, trace: &StackTrace) -> Result<(), Error>;
    fn write(&self, w: &mut dyn Write) -> Result<(), Error>;
}

impl Recorder for speedscope::Stats {
    fn increment(&mut self, trace: &StackTrace) -> Result<(), Error> {
        // println!("{:?}", trace);
        Ok(self.record(trace)?)
    }
    fn write(&self, w: &mut dyn Write) -> Result<(), Error> {
        self.write(w)
    }
}


pub struct InternalProfiler {
    profiler_thread: Option<thread::JoinHandle<Result<std::string::String, Error>>>,
    running: Arc<AtomicBool>,
}

impl InternalProfiler {
    pub fn new(pid: remoteprocess::Pid) -> Self {

        let args = ["p".to_string(), "record".to_string(), "-p".to_string(), pid.to_string(), "-f".to_string(), "speedscope".to_string(), "--nonblocking".to_string(), "-r".to_string(), "1000".to_string()];
        let config = Config::from_args(&args).unwrap();
        let sampling_rate = config.sampling_rate as f64;

        let ready = Arc::new(AtomicBool::new(false));
        let ready_clone = ready.clone();
        let running = Arc::new(AtomicBool::new(true));
        let running_clone = running.clone();
        let profiler_thread = thread::spawn(move || {
            InternalProfiler::record_samples(pid, &config, running_clone, ready_clone)
        });
        for _sleep in Timer::new(sampling_rate as f64) {
            if ready.load(Ordering::SeqCst) {
                break;
            }
        }

        Self { profiler_thread: Some(profiler_thread), running }
    }

    fn record_samples(pid: remoteprocess::Pid, config: &Config, running: Arc<AtomicBool>, ready: Arc<AtomicBool>) -> Result<std::string::String, Error> {
        let mut output: Box<dyn Recorder> = match config.format {
            Some(FileFormat::flamegraph) => return Err(format_err!("Flamegraph not supported")),
            Some(FileFormat::speedscope) => Box::new(speedscope::Stats::new(config)),
            Some(FileFormat::raw) => return Err(format_err!("Raw not supported")),
            Some(FileFormat::chrometrace) => return Err(format_err!("Chrometrace not supported")),
            None => return Err(format_err!("A file format is required to record samples")),
        };

        let sampler = sampler::Sampler::new(pid, config)?;

        // if we're not showing a progress bar, it's probably because we've spawned the process and
        // are displaying its stderr/stdout. In that case add a prefix to our println messages so
        // that we can distinguish
        let lede = if config.hide_progress {
            format!("{}{} ", style("py-spy").bold().green(), style(">").dim())
        } else {
            "".to_owned()
        };

        let mut errors = 0;
        let mut samples = 0;

        // let running = Arc::new(AtomicBool::new(false));
        // let r: Arc<AtomicBool> = running.clone();
        // ctrlc::set_handler(move || {
        //     r.store(false, Ordering::SeqCst);
        // })?;

        let mut last_late_message = std::time::Instant::now();

        ready.store(true, Ordering::SeqCst);
        for mut sample in sampler {
            if let Some(delay) = sample.late {
                if delay > Duration::from_secs(1) {
                    if config.hide_progress {
                        // display a message if we're late, but don't spam the log
                        let now = std::time::Instant::now();
                        if now - last_late_message > Duration::from_secs(1) {
                            last_late_message = now;
                            println!("{lede}{delay:.2?} behind in sampling, results may be inaccurate. Try reducing the sampling rate")
                        }
                    } else {
                        let term = console::Term::stdout();
                        term.move_cursor_up(2)?;
                        println!("{delay:.2?} behind in sampling, results may be inaccurate. Try reducing the sampling rate.");
                        term.move_cursor_down(1)?;
                    }
                }
            }

            if !running.load(Ordering::SeqCst) {
                break;
            }

            for trace in sample.traces.iter_mut() {
                if !(config.include_idle || trace.active) {
                    continue;
                }

                if config.gil_only && !trace.owns_gil {
                    continue;
                }

                if config.include_thread_ids {
                    let threadid = trace.format_threadid();
                    let thread_fmt = if let Some(thread_name) = &trace.thread_name {
                        format!("thread ({threadid}): {thread_name}")
                    } else {
                        format!("thread ({threadid})")
                    };
                    trace.frames.push(Frame {
                        name: thread_fmt,
                        filename: String::from(""),
                        module: None,
                        short_filename: None,
                        line: 0,
                        locals: None,
                        is_entry: true,
                        is_shim_entry: true,
                    });
                }

                if let Some(process_info) = trace.process_info.as_ref() {
                    trace.frames.push(process_info.to_frame());
                    let mut parent = process_info.parent.as_ref();
                    while parent.is_some() {
                        if let Some(process_info) = parent {
                            trace.frames.push(process_info.to_frame());
                            parent = process_info.parent.as_ref();
                        }
                    }
                }

                samples += 1;
                output.increment(trace)?;
            }

            if let Some(sampling_errors) = sample.sampling_errors {
                for (pid, e) in sampling_errors {
                    warn!("Failed to get stack trace from {}: {}", pid, e);
                    errors += 1;
                }
            }

            if config.duration == RecordDuration::Unlimited {
                let msg = if errors > 0 {
                    format!("Collected {samples} samples ({errors} errors)")
                } else {
                    format!("Collected {samples} samples")
                };
            }
        }

        
        let mut write_buffer = Vec::new();
        output.write(&mut write_buffer)?;
        

        Ok(std::str::from_utf8(write_buffer.as_slice()).unwrap().to_string())
    }

    fn finish(&mut self) -> Result<String, Error> {
        self.running.store(false, Ordering::SeqCst);
        let mut profiler_thread = Option::None;
        mem::swap(&mut self.profiler_thread, &mut profiler_thread);
        if profiler_thread.is_none() {
            return Result::Err(format_err!("No Running Thread"))
        }
        let thread_result = profiler_thread.unwrap().join();
        return match thread_result {
            Ok(unpacked_result) => {
                match unpacked_result {
                    Ok(file_str) => Result::Ok(file_str.to_owned()),
                    Err(error) => Result::Err(error),
                }
            },
            Err(_error) => Result::Err(format_err!("Failed to join profiling thread")),
        };
    }

}

#[pyclass]
pub struct PySpyProfiler {
    profiler: InternalProfiler,
}

#[pymethods]
impl PySpyProfiler {
    #[new]
    fn new(pid: i32) -> Self {
        Self {
            profiler: InternalProfiler::new(pid),
        }
    }

    fn finish(&mut self) -> PyResult<String> {
        match self.profiler.finish() {
            Ok(file_str) => PyResult::Ok(file_str.to_owned()),
            Err(error) => PyResult::Err(PyRuntimeError::new_err(error.to_string())),
        }
    }
}

impl Drop for PySpyProfiler {
    fn drop(&mut self) {
        let _ = self.finish();
    }
}


/// A Python module implemented in Rust. The name of this function must match
/// the `lib.name` setting in the `Cargo.toml`, else Python will not be able to
/// import the module.
#[pymodule]
fn py_spy_monitor(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PySpyProfiler>()?;
    Ok(())
}