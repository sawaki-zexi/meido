// Test-only actual process launcher. It never connects to a model or account.
fn main() {
    let python = std::env::var("MEIDO_CODING_FIXTURE_PYTHON").expect("explicit fixture Python");
    let script = std::env::var("MEIDO_CODING_FIXTURE_SCRIPT").expect("explicit fixture script");
    let status = std::process::Command::new(python)
        .args(["-X", "utf8", &script])
        .args(std::env::args().skip(1))
        .status().expect("fixture process");
    std::process::exit(status.code().unwrap_or(1));
}
