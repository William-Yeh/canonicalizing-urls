//! Render benchmark figures from measured criterion medians (feature = "figures").
//!
//! A dev-only tool, kept as an example (not a `[[bin]]`) so release tooling
//! does not package it. Numbers are transcribed from a representative
//! `cargo bench` run (Apple M-series). Re-run `cargo bench`, update the arrays
//! below, then:
//!   `cargo run --example gen_figures --features figures`
//! Regenerates `figures/bench{1..4}.png` + `figures/bench_overview.png`.

use plotters::prelude::*;

/// (x, indexed/primary series, naive/secondary series-or-None)
struct Series {
    title: &'static str,
    x_label: &'static str,
    primary_label: &'static str,
    secondary_label: Option<&'static str>,
    xs: Vec<f64>,
    primary: Vec<f64>,           // microseconds
    secondary: Option<Vec<f64>>, // microseconds
}

fn series() -> [Series; 4] {
    [
        Series {
            title: "Bench 1: exact-host lookup (O(1) vs O(R))",
            x_label: "R (rules)",
            primary_label: "index lookup",
            secondary_label: Some("naive .matches()"),
            xs: vec![10.0, 100.0, 1000.0, 5000.0],
            primary: vec![0.0454, 0.0447, 0.0457, 0.0454],
            secondary: Some(vec![0.0263, 0.2379, 2.3208, 11.682]),
        },
        Series {
            title: "Bench 2: HostGlob RegexSet (O(L), ~flat in G)",
            x_label: "G (glob rules)",
            primary_label: "RegexSet candidate lookup",
            secondary_label: None,
            xs: vec![5.0, 20.0, 50.0, 100.0],
            primary: vec![0.0604, 0.0694, 0.0811, 0.1034],
            secondary: None,
        },
        Series {
            title: "Bench 3: index build O(R) vs lookup O(1)",
            x_label: "R (rules)",
            primary_label: "build",
            secondary_label: Some("lookup"),
            xs: vec![50.0, 200.0, 1000.0, 5000.0],
            primary: vec![6.2455, 25.670, 145.83, 687.49],
            secondary: Some(vec![0.0450, 0.0460, 0.0447, 0.0450]),
        },
        Series {
            title: "Bench 4: pipeline canonicalize() over R rules",
            x_label: "R (rules)",
            primary_label: "canonicalize",
            secondary_label: None,
            xs: vec![10.0, 50.0, 200.0, 500.0],
            primary: vec![2.3496, 7.4685, 27.498, 76.120],
            secondary: None,
        },
    ]
}

fn draw<DB: DrawingBackend>(area: &DrawingArea<DB, plotters::coord::Shift>, s: &Series)
where
    DB::ErrorType: 'static,
{
    let ys: Vec<f64> = s
        .primary
        .iter()
        .chain(s.secondary.iter().flatten())
        .copied()
        .collect();
    let (ymin, ymax) = (
        ys.iter().cloned().fold(f64::MAX, f64::min) * 0.7,
        ys.iter().cloned().fold(f64::MIN, f64::max) * 1.4,
    );
    let (xmin, xmax) = (s.xs[0] * 0.7, s.xs[s.xs.len() - 1] * 1.4);

    let mut chart = ChartBuilder::on(area)
        .caption(s.title, ("sans-serif", 18))
        .margin(10)
        .x_label_area_size(40)
        .y_label_area_size(60)
        .build_cartesian_2d((xmin..xmax).log_scale(), (ymin..ymax).log_scale())
        .unwrap();
    chart
        .configure_mesh()
        .x_desc(s.x_label)
        .y_desc("µs (log)")
        .draw()
        .unwrap();

    chart
        .draw_series(LineSeries::new(
            s.xs.iter().zip(&s.primary).map(|(&x, &y)| (x, y)),
            BLUE.stroke_width(2),
        ))
        .unwrap()
        .label(s.primary_label)
        .legend(|(x, y)| PathElement::new(vec![(x, y), (x + 18, y)], BLUE));
    chart
        .draw_series(
            s.xs.iter()
                .zip(&s.primary)
                .map(|(&x, &y)| Circle::new((x, y), 3, BLUE.filled())),
        )
        .unwrap();

    if let (Some(sec), Some(label)) = (&s.secondary, s.secondary_label) {
        chart
            .draw_series(LineSeries::new(
                s.xs.iter().zip(sec).map(|(&x, &y)| (x, y)),
                RED.stroke_width(2),
            ))
            .unwrap()
            .label(label)
            .legend(|(x, y)| PathElement::new(vec![(x, y), (x + 18, y)], RED));
        chart
            .draw_series(
                s.xs.iter()
                    .zip(sec)
                    .map(|(&x, &y)| Circle::new((x, y), 3, RED.filled())),
            )
            .unwrap();
    }
    chart
        .configure_series_labels()
        .background_style(WHITE)
        .border_style(BLACK)
        .draw()
        .unwrap();
}

fn main() {
    std::fs::create_dir_all("figures").unwrap();
    let data = series();

    for (i, s) in data.iter().enumerate() {
        let path = format!("figures/bench{}.png", i + 1);
        let root = BitMapBackend::new(&path, (640, 480)).into_drawing_area();
        root.fill(&WHITE).unwrap();
        draw(&root, s);
        root.present().unwrap();
        println!("wrote {path}");
    }

    // 2×2 overview.
    let root = BitMapBackend::new("figures/bench_overview.png", (1280, 960)).into_drawing_area();
    root.fill(&WHITE).unwrap();
    let panels = root.split_evenly((2, 2));
    for (panel, s) in panels.iter().zip(&data) {
        draw(panel, s);
    }
    root.present().unwrap();
    println!("wrote figures/bench_overview.png");
}
